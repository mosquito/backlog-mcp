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
  return globalThis.agentConfigurations({ endpoint: url.href, name, headers, level });
}
