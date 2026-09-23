import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { agentConfig } from "../src/backlog_mcp/static/agent-config.js";

const endpoint = "https://backlog.example:8443/mcp";
test("all clients use the same endpoint and UTF-8 Basic credentials", () => {
  const data = agentConfig({ endpoint, username: "пользователь", secret: 'p:a\"ss🔑' });
  const claude = JSON.parse(data.claude.config).mcpServers.backlog;
  const antigravity = JSON.parse(data.antigravity.config).mcpServers.backlog;
  assert.equal(claude.url, endpoint);
  assert.equal(claude.type, "http");
  assert.equal(antigravity.serverUrl, endpoint);
  assert.deepEqual(antigravity.headers, claude.headers);
  assert.equal(
    Buffer.from(claude.headers.Authorization.slice(6), "base64").toString(),
    'пользователь:p:a\"ss🔑',
  );
  const parsed = spawnSync(
    "python3",
    ["-c", "import tomllib,json,sys; print(json.dumps(tomllib.loads(sys.stdin.read())))"],
    { input: data.codex.config, encoding: "utf8" },
  );
  assert.equal(parsed.status, 0, parsed.stderr);
  assert.deepEqual(JSON.parse(parsed.stdout).mcp_servers.backlog.http_headers, claude.headers);
});
test("shell commands preserve quotes and substitution syntax as literal arguments", () => {
  const secret = `token'\"$(printf injected)\`printf injected\``;
  const data = agentConfig({ endpoint, method: "bearer", secret });
  for (const [name, config] of Object.entries(data).filter(([, c]) => c.command)) {
    const command = `${name}() { printf '%s\\0' "$@"; }; ${config.command}`;
    const result = spawnSync("/bin/sh", ["-c", command], { encoding: "utf8" });
    assert.equal(result.status, 0, result.stderr);
    const args = result.stdout.split("\0").filter(Boolean);
    if (name === "claude")
      assert.equal(args[args.indexOf("--header") + 1], "Authorization: Bearer " + secret);
    else {
      const parsed = spawnSync(
        "python3",
        ["-c", "import tomllib,json,sys; print(json.dumps(tomllib.loads(sys.stdin.read())))"],
        { input: args.filter((_, i) => i % 2 === 1).join("\n"), encoding: "utf8" },
      );
      assert.equal(parsed.status, 0, parsed.stderr);
      assert.equal(
        JSON.parse(parsed.stdout).mcp_servers.backlog.http_headers.Authorization,
        "Bearer " + secret,
      );
    }
  }
});
test("no-auth output omits headers and invalid credentials fail", () => {
  const data = agentConfig({ endpoint, method: "none" });
  assert.equal(JSON.parse(data.claude.config).mcpServers.backlog.headers, undefined);
  assert.ok(!data.codex.config.includes("http_headers"));
  for (const input of [
    { username: "a:b", secret: "c" },
    { username: "a", secret: "" },
    { method: "bearer", secret: "a\nb" },
    { method: "bearer", secret: " " },
    { endpoint: "javascript:alert(1)", method: "none" },
    { endpoint: "https://user:pass@example.test/mcp", method: "none" },
  ])
    assert.throws(() => agentConfig({ endpoint, ...input }));
});
