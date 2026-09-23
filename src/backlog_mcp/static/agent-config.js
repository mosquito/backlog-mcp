/** Generate configuration text without accessing storage or the network. */
export function shellQuote(value) {
  return "'" + value.replaceAll("'", "'\"'\"'") + "'";
}

export function agentConfig({ endpoint, method = "basic", username = "", secret = "" }) {
  const url = new URL(endpoint);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password)
    throw new Error("Use an HTTP or HTTPS MCP URL without embedded credentials.");
  let authorization = "";
  if (method === "basic") {
    if (!username || !secret) throw new Error("Enter your username and password.");
    if (username.includes(":")) throw new Error("The username cannot contain a colon.");
    const bytes = new TextEncoder().encode(`${username}:${secret}`);
    authorization = "Basic " + btoa(Array.from(bytes, (b) => String.fromCharCode(b)).join(""));
  } else if (method === "bearer") {
    if (!secret.trim() || /[\r\n]/.test(secret))
      throw new Error("Enter a token without line breaks.");
    authorization = "Bearer " + secret.trim();
  } else if (method !== "none") throw new Error("Unknown authentication method.");
  const headers = authorization ? { Authorization: authorization } : {};
  const http = { type: "http", url: url.href, ...(authorization ? { headers } : {}) };
  const toml =
    `url = ${JSON.stringify(url.href)}\n` +
    (authorization ? `http_headers = { Authorization = ${JSON.stringify(authorization)} }\n` : "");
  return {
    claude: {
      config: JSON.stringify({ mcpServers: { backlog: http } }, null, 2),
      command:
        `claude mcp add --scope user --transport http` +
        (authorization ? ` --header ${shellQuote("Authorization: " + authorization)}` : "") +
        ` backlog ${shellQuote(url.href)}`,
    },
    codex: {
      config: `[mcp_servers.backlog]\n${toml}`,
      command:
        "codex -c " +
        shellQuote(`mcp_servers.backlog.url=${JSON.stringify(url.href)}`) +
        (authorization
          ? " -c " +
            shellQuote(
              `mcp_servers.backlog.http_headers={ Authorization = ${JSON.stringify(authorization)} }`,
            )
          : ""),
    },
    antigravity: {
      config: JSON.stringify(
        {
          mcpServers: {
            backlog: {
              serverUrl: url.href,
              ...(authorization ? { headers } : {}),
            },
          },
        },
        null,
        2,
      ),
    },
  };
}
