import { beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import {
  authorization,
  clearCredentials,
  loadCredentials,
  saveCredentials,
} from "../src/backlog_mcp/static/credentials.js";

const origin = "https://backlog.test";
const endpoint = `${origin}/mcp`;

function storage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  };
}

function consoleKey(path = "/console", address = endpoint) {
  return `mcp-console-credentials:${JSON.stringify([path, address])}`;
}

function record(fields = {}) {
  return {
    version: 1,
    endpoint,
    method: "basic",
    username: "user",
    secret: "password",
    header: "",
    ...fields,
  };
}

beforeEach(() => {
  globalThis.location = { origin, href: `${origin}/ui/`, pathname: "/ui/" };
  globalThis.localStorage = storage();
  globalThis.sessionStorage = storage();
});

test("saved credentials use the canonical console format and remove stale aliases", () => {
  const input = { method: "basic", username: "user", secret: "password" };
  localStorage.setItem(consoleKey("/console/"), JSON.stringify(record({ secret: "old" })));
  saveCredentials("/mcp", input);
  assert.deepEqual(JSON.parse(localStorage.getItem(consoleKey())), record());
  assert.equal(localStorage.getItem(consoleKey("/console/")), null);
  assert.deepEqual(loadCredentials("/mcp"), record());
  assert.equal(localStorage.getItem("backlog:auth:/mcp"), null);
  assert.equal(sessionStorage.getItem("backlog:auth:/mcp"), null);
});

test("console records restore from either console path on UI and connect pages", () => {
  for (const path of ["/console", "/console/"]) {
    for (const page of ["/ui/", "/ui/connect"]) {
      globalThis.localStorage = storage();
      globalThis.location.pathname = page;
      globalThis.location.href = origin + page;
      localStorage.setItem(consoleKey(path), JSON.stringify(record()));
      assert.deepEqual(loadCredentials("/mcp"), record());
      assert.equal(authorization(loadCredentials("/mcp")), "Basic dXNlcjpwYXNzd29yZA==");
    }
  }
});

test("Basic authorization preserves UTF-8 and colons in passwords", () => {
  const input = { method: "basic", username: "пользователь", secret: "p:a:ss🔑" };
  saveCredentials("/mcp", input);
  const header = authorization(loadCredentials("/mcp"));
  assert.ok(header.startsWith("Basic "));
  assert.equal(Buffer.from(header.slice(6), "base64").toString("utf8"), "пользователь:p:a:ss🔑");
});

test("Bearer credentials round trip without Basic encoding", () => {
  const input = { method: "bearer", username: "", secret: "abc.DEF_123-~+/==" };
  saveCredentials("/mcp", input);
  assert.deepEqual(loadCredentials("/mcp"), record(input));
  assert.equal(authorization(loadCredentials("/mcp")), `Bearer ${input.secret}`);
  assert.equal(authorization(null), "");
});

test("credentials remain scoped to the exact endpoint and query", () => {
  const input = { method: "bearer", username: "", secret: "token" };
  saveCredentials("/mcp?project=owner%2Frepo", input);
  assert.equal(loadCredentials("/mcp"), null);
  assert.equal(loadCredentials("/mcp?project=owner%2Fother"), null);
  assert.equal(loadCredentials("/other"), null);
  assert.equal(loadCredentials("https://other.test/mcp?project=owner%2Frepo"), null);
  assert.equal(authorization(loadCredentials("/mcp?project=owner%2Frepo")), "Bearer token");
});

test("malformed and unsupported console records do not become credentials", () => {
  const invalid = [
    "not json",
    "null",
    "[]",
    JSON.stringify(record({ version: 2 })),
    JSON.stringify(record({ endpoint: `${endpoint}?project=other` })),
    JSON.stringify(record({ method: "custom", header: "X-API-Key" })),
    JSON.stringify(record({ username: null })),
    JSON.stringify(record({ secret: 123 })),
    JSON.stringify(record({ username: "a:b" })),
    JSON.stringify(record({ secret: "line\nbreak" })),
    JSON.stringify(record({ method: "bearer", secret: "contains spaces" })),
  ];
  for (const value of invalid) {
    localStorage.setItem(consoleKey(), value);
    assert.equal(loadCredentials("/mcp"), null, value);
  }
});

test("authorization rejects credentials that cannot form valid headers", () => {
  for (const fields of [
    { username: "a:b" },
    { username: "a\nb" },
    { secret: "a\rb" },
    { secret: "a\u007fb" },
    { method: "bearer", secret: "" },
    { method: "bearer", secret: " token" },
    { method: "bearer", secret: "token\n" },
    { method: "bearer", secret: "токен" },
  ])
    assert.throws(() => authorization(record(fields)));
});

test("logout clears both console aliases and legacy credentials without clearing other endpoints", () => {
  const saved = JSON.stringify(record());
  for (const path of ["/console", "/console/"])
    localStorage.setItem(consoleKey(path), saved);
  localStorage.setItem("backlog:auth:/mcp", "Bearer old-local");
  sessionStorage.setItem("backlog:auth:/mcp", "Bearer old-session");
  const otherKey = consoleKey("/console", `${origin}/other`);
  localStorage.setItem(otherKey, "unrelated");
  clearCredentials("/mcp");
  for (const path of ["/console", "/console/"])
    assert.equal(localStorage.getItem(consoleKey(path)), null);
  assert.equal(localStorage.getItem("backlog:auth:/mcp"), null);
  assert.equal(sessionStorage.getItem("backlog:auth:/mcp"), null);
  assert.equal(localStorage.getItem(otherKey), "unrelated");
  assert.equal(loadCredentials("/mcp"), null);
});

test("blocked storage leaves manual authentication available", () => {
  const blocked = () => { throw new Error("Storage is blocked"); };
  globalThis.localStorage = { getItem: blocked, setItem: blocked, removeItem: blocked };
  globalThis.sessionStorage = { getItem: blocked, setItem: blocked, removeItem: blocked };
  assert.equal(loadCredentials("/mcp"), null);
  assert.doesNotThrow(() => saveCredentials("/mcp", {
    method: "bearer", username: "", secret: "token",
  }));
  assert.doesNotThrow(() => clearCredentials("/mcp"));
  assert.equal(authorization(record({ method: "bearer", secret: "token" })), "Bearer token");
});
