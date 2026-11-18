import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { runInThisContext } from "node:vm";
import { agentConfig } from "../src/backlog_mcp/static/agent-config.js";

const upstream = execFileSync("uv", [
  "run", "python", "-c",
  "from aiohttp_tiny_mcp.console import Console; print(Console().render('agent-config.js'))",
], { encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
runInThisContext(upstream, { filename: "console-agent-config.js" });

const computer = "workstation";
const endpoint = "https://backlog.example:8443/mcp";
const targets = {
  "mcp-json": ["~/.claude.json", ".mcp.json"],
  codex: ["~/.codex/config.toml", ".codex/config.toml"],
  cursor: ["~/.cursor/mcp.json", ".cursor/mcp.json"],
  copilot: ["~/.copilot/mcp-config.json", ".mcp.json"],
  gemini: ["~/.gemini/settings.json", ".gemini/settings.json"],
  pi: ["~/.pi/agent/mcp.json", ".pi/mcp.json"],
  opencode: ["~/.config/opencode/opencode.json", "opencode.json"],
};

function parseServer(variant, name = "backlog") {
  if (variant.language === "toml") {
    const result = execFileSync("uv", [
      "run", "python", "-c",
      "import json,sys,tomllib; print(json.dumps(tomllib.loads(sys.stdin.read())))",
    ], { input: variant.code, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
    return JSON.parse(result).mcp_servers[name];
  }
  const result = JSON.parse(variant.code);
  return variant.id === "opencode" ? result.mcp.servers[name] : result.mcpServers[name];
}

function serverEndpoint(server) {
  return server.url || server.httpUrl || server.serverUrl;
}

function serverHeaders(server) {
  return server.headers || server.http_headers;
}

test("all seven console agents have user and project instructions", () => {
  for (const level of ["user", "project"]) {
    const variants = agentConfig({ endpoint, computer, method: "none", level });
    const expected = Object.keys(targets);
    if (level === "user") expected.push("antigravity");
    assert.deepEqual(variants.map((variant) => variant.id), expected);
    for (const variant of variants) {
      assert.ok(variant.label);
      assert.ok(variant.instructions);
      assert.ok(variant.verify);
      assert.ok(variant.link.startsWith("https://"));
      assert.ok(["json", "toml"].includes(variant.language));
      if (variant.id === "antigravity") {
        assert.equal(variant.target, "mcp_config.json");
        assert.match(variant.instructions, /raw config/i);
      } else {
        assert.equal(variant.target, targets[variant.id][level === "user" ? 0 : 1]);
        assert.ok(variant.instructions.includes(variant.target));
      }
      const server = parseServer(variant);
      assert.equal(serverEndpoint(server), endpoint + "?computer=workstation");
      assert.equal(serverHeaders(server), undefined);
    }
  }
});

test("all agents preserve custom server names and UTF-8 Basic credentials", () => {
  const name = "team-backlog";
  const username = "пользователь";
  const secret = 'p:a\\"ss🔑';
  const variants = agentConfig({ endpoint, computer, name, username, secret });
  for (const variant of variants) {
    const server = parseServer(variant, name);
    assert.equal(serverEndpoint(server), endpoint + "?computer=workstation");
    const headers = serverHeaders(server);
    assert.deepEqual(Object.keys(headers), ["Authorization"]);
    assert.ok(headers.Authorization.startsWith("Basic "));
    assert.equal(
      Buffer.from(headers.Authorization.slice(6), "base64").toString("utf8"),
      `${username}:${secret}`,
    );
  }
});

test("all agents support Bearer credentials and can omit them from shared examples", () => {
  const secret = "abc.DEF_123-~+/==";
  for (const includeCredentials of [true, false]) {
    const variants = agentConfig({
      endpoint, computer, method: "bearer", secret, includeCredentials,
    });
    for (const variant of variants) {
      const server = parseServer(variant);
      assert.deepEqual(
        serverHeaders(server),
        includeCredentials ? { Authorization: `Bearer ${secret}` } : undefined,
      );
      assert.equal(variant.code.includes(secret), includeCredentials);
    }
  }
});

test("omitting Basic credentials removes authentication from every agent", () => {
  const variants = agentConfig({
    endpoint, computer, username: "user", secret: "private", includeCredentials: false,
  });
  for (const variant of variants) assert.equal(serverHeaders(parseServer(variant)), undefined);
});

test("computer is required and replaces only its own URL parameter", () => {
  for (const computer of [undefined, "", "  "])
    assert.throws(() => agentConfig({ endpoint, computer, method: "none" }), /computer name/i);
  const variants = agentConfig({
    endpoint: endpoint + "?project=owner%2Frepo&computer=old&computer=duplicate&label=a&label=b",
    computer: "  laptop & стол  ",
    method: "none",
  });
  for (const variant of variants) {
    const url = new URL(serverEndpoint(parseServer(variant)));
    assert.equal(url.searchParams.get("project"), "owner/repo");
    assert.deepEqual(url.searchParams.getAll("computer"), ["laptop & стол"]);
    assert.deepEqual(url.searchParams.getAll("label"), ["a", "b"]);
  }
});

test("invalid connection details and configuration options fail", () => {
  for (const input of [
    { username: "a:b", secret: "c" },
    { username: "a", secret: "" },
    { username: "a\nb", secret: "c" },
    { method: "bearer", secret: "a\nb" },
    { method: "bearer", secret: " " },
    { method: "unknown" },
    { endpoint: "javascript:alert(1)", method: "none" },
    { endpoint: "https://user:pass@example.test/mcp", method: "none" },
    { endpoint: endpoint + "#fragment", method: "none" },
    { name: "bad name", method: "none" },
    { name: "", method: "none" },
    { level: "workspace", method: "none" },
  ])
    assert.throws(() => agentConfig({ endpoint, computer, ...input }));
});
