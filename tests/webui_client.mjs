import { test } from "node:test";
import assert from "node:assert/strict";
import { MCP, HttpError, RpcError, ToolError } from "../src/backlog_mcp/static/mcp.js";
import { route } from "../src/backlog_mcp/static/dom.js";
import { changed } from "../src/backlog_mcp/static/editor.js";

globalThis.location = { origin: "https://backlog.test" };
const json = (id, result, headers = {}) =>
  new Response(JSON.stringify({ jsonrpc: "2.0", id, result }), {
    headers: { "Content-Type": "application/json", ...headers },
  });
function transport(reply) {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    calls.push({ url: String(url), options, body });
    if (body.method === "initialize")
      return json(
        body.id,
        { protocolVersion: "2025-11-25" },
        { "Mcp-Session-Id": "session-" + calls.length },
      );
    if (body.method === "notifications/initialized") return new Response(null, { status: 202 });
    return reply(body, options, calls);
  };
  return calls;
}
test("handshake, explicit scope and credentials", async () => {
  const calls = transport((body) =>
    json(body.id, { content: [{ type: "text", text: '{"ok":true}' }] }),
  );
  const client = new MCP("/mcp", "Bearer secret");
  assert.deepEqual(await client.call("task_list", { project: "owner/repo" }), { ok: true });
  assert.deepEqual(
    calls.map((c) => c.body.method),
    ["initialize", "notifications/initialized", "tools/call"],
  );
  const request = calls[2];
  assert.equal(request.url, "https://backlog.test/mcp");
  assert.equal(request.options.headers.Authorization, "Bearer secret");
  assert.equal(request.options.headers["Mcp-Session-Id"], "session-1");
  assert.equal(request.options.headers["MCP-Protocol-Version"], "2025-11-25");
  assert.equal(request.options.credentials, "omit");
  assert.equal(request.options.redirect, "error");
  assert.equal(request.body.params.arguments.project, "owner/repo");
});
test("expired sessions reinitialize and retry once", async () => {
  let attempts = 0;
  const calls = transport((body) =>
    ++attempts === 1
      ? new Response("", { status: 404 })
      : json(body.id, { content: [{ type: "text", text: "{}" }] }),
  );
  await new MCP("/mcp").call("whoami");
  assert.equal(calls.filter((c) => c.body.method === "initialize").length, 2);
  assert.equal(attempts, 2);
});
test("network failures do not retry writes", async () => {
  let attempts = 0;
  transport(() => {
    attempts++;
    throw new TypeError("offline");
  });
  await assert.rejects(new MCP("/mcp").call("task_add"), TypeError);
  assert.equal(attempts, 1);
});
test("SSE accepts fragmented CRLF multiline data and notifications", async () => {
  transport((body) => {
    const text =
      ': keepalive\r\n\r\ndata: {"jsonrpc":"2.0","method":"notice"}\r\n\r\n' +
      `data: {"jsonrpc":"2.0","id":${body.id},\r\ndata: "result":{"content":[{"type":"text","text":"{\\"name\\":\\"Привет\\"}"}]}}\r\n\r\n`;
    const bytes = new TextEncoder().encode(text);
    return new Response(
      new ReadableStream({
        start(controller) {
          for (let i = 0; i < bytes.length; i += 3) controller.enqueue(bytes.slice(i, i + 3));
          controller.close();
        },
      }),
      { headers: { "Content-Type": "text/event-stream" } },
    );
  });
  assert.deepEqual(await new MCP("/mcp").call("whoami"), { name: "Привет" });
});
test("RPC and tool failures remain distinguishable", async () => {
  transport(
    (body) =>
      new Response(
        JSON.stringify({
          jsonrpc: "2.0",
          id: body.id,
          error: { code: -32602, message: "Bad params" },
        }),
      ),
  );
  await assert.rejects(
    new MCP("/mcp").call("task_get"),
    (e) => e instanceof RpcError && e.code === -32602,
  );
  transport((body) =>
    json(body.id, { isError: true, content: [{ type: "text", text: "Permission denied" }] }),
  );
  await assert.rejects(
    new MCP("/mcp").call("task_get"),
    (e) => e instanceof ToolError && e.message === "Permission denied",
  );
});
test("401 exposes discovery without retry", async () => {
  transport(
    () =>
      new Response("", {
        status: 401,
        headers: { "WWW-Authenticate": 'Basic realm="test", charset="UTF-8", Bearer realm="test"' },
      }),
  );
  await assert.rejects(
    new MCP("/mcp").call("whoami"),
    (e) => e instanceof HttpError && e.schemes.join(",") === "basic,bearer",
  );
});
test("cross-origin endpoints are refused", () => {
  assert.throws(() => new MCP("https://other.test/mcp"));
});
test("routes preserve project and filter state", () => {
  const parsed = route("#/p/alice/project?tag=needs+review&priority=high");
  assert.equal(parsed.project, "alice/project");
  assert.equal(parsed.params.get("tag"), "needs review");
  assert.equal(route("#/t/new?project=alice%2Fproject").kind, "new");
  assert.equal(route("#/t/42/edit").id, 42);
});
test("editing sends only changed fields", () => {
  const base = { title: "Task", status: "todo", priority: "normal", body: "Text", tags: ["a"] };
  assert.deepEqual(changed(base, { ...base, title: "Edited" }), { title: "Edited" });
  assert.deepEqual(changed(base, { ...base, tags: [] }), { tags: [] });
});
