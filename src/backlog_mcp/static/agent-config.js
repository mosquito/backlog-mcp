import { authorization } from "./credentials.js";

export function agentConfig({
  endpoint, computer = "", method = "basic", username = "", secret = "",
  name = "backlog", level = "user", includeCredentials = true,
}) {
  const url = new URL(endpoint);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || endpoint.includes("#"))
    throw new Error("Use an HTTP or HTTPS MCP URL without embedded credentials or a fragment.");
  if (!computer.trim()) throw new Error("Enter your computer name.");
  url.searchParams.set("computer", computer.trim());
  let headers = {};
  if (includeCredentials && method !== "none") {
    if (method === "basic" && (!username || !secret))
      throw new Error("Enter your username and password.");
    const token = method === "bearer" ? secret.trim() : secret;
    headers = { Authorization: authorization({ method, username, secret: token }) };
  }
  const variants = globalThis.agentConfigurations({ endpoint: url.href, name, headers, level });
  if (level === "user") {
    variants.push({
      id: "antigravity", label: "Antigravity", target: "mcp_config.json", language: "json",
      instructions: "In Antigravity, open MCP Servers → Manage MCP Servers → View raw config. Merge this server entry with existing settings.",
      code: JSON.stringify({
        mcpServers: { [name]: { serverUrl: url.href, ...(Object.keys(headers).length ? { headers } : {}) } },
      }, null, 2),
      verify: "Refresh the MCP servers list in Antigravity and check the server.",
      link: "https://antigravity.google/docs/mcp",
    });
  }
  return variants;
}
