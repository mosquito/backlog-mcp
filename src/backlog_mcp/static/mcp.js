/** Minimal Streamable HTTP client. Credentials only leave through this endpoint. */
export class HttpError extends Error {
  constructor(response) {
    super(
      response.status === 401
        ? "Sign in to continue. Check your credentials."
        : `HTTP ${response.status}: ${response.statusText}`,
    );
    this.status = response.status;
    this.schemes = [
      ...(response.headers.get("WWW-Authenticate") || "").matchAll(/(?:^|,)\s*(Basic|Bearer)\b/gi),
    ].map((m) => m[1].toLowerCase());
  }
}
export class RpcError extends Error {
  constructor(error) {
    super(error.message);
    this.code = error.code;
    this.data = error.data;
  }
}
export class ToolError extends Error {}

export class MCP {
  constructor(endpoint, authorization = "") {
    this.url = new URL(endpoint, location.origin);
    if (this.url.origin !== location.origin || this.url.username || this.url.password)
      throw new Error("The MCP endpoint must use this origin.");
    this.authorization = authorization;
    this.session = null;
    this.counter = 0;
    this.ready = null;
    this.version = "2025-11-25";
    this.abort = new AbortController();
  }
  close() {
    this.abort.abort();
    this.session = null;
    this.ready = null;
  }
  async send(method, params = {}, notification = false) {
    const id = notification ? undefined : ++this.counter;
    const headers = {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      "MCP-Protocol-Version": this.version,
    };
    if (this.authorization) headers.Authorization = this.authorization;
    if (this.session) headers["Mcp-Session-Id"] = this.session;
    const response = await fetch(this.url, {
      method: "POST",
      headers,
      credentials: "omit",
      redirect: "error",
      signal: this.abort.signal,
      body: JSON.stringify({ jsonrpc: "2.0", id, method, params }),
    });
    if (!response.ok) {
      if (response.status === 401 || response.status === 404) {
        await response.body?.cancel();
        throw new HttpError(response);
      }
      const body = await response.json().catch(() => null);
      if (body?.error) throw new RpcError(body.error);
      throw new HttpError(response);
    }
    const session = response.headers.get("Mcp-Session-Id");
    if (session) this.session = session;
    if (notification) {
      await response.body?.cancel();
      return;
    }
    const match = (frame) => frame.id === id || (frame.id === null && frame.error);
    const unwrap = (frame) => {
      if (frame.error) throw new RpcError(frame.error);
      return frame.result;
    };
    if (!(response.headers.get("Content-Type") || "").includes("text/event-stream")) {
      const frame = await response.json();
      if (!match(frame)) throw new Error("MCP returned an unexpected response ID.");
      return unwrap(frame);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "",
      data = [];
    try {
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        let cut;
        while ((cut = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, cut).replace(/\r$/, "");
          buffer = buffer.slice(cut + 1);
          if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
          if (line === "" && data.length) {
            const text = data.join("\n");
            data = [];
            if (!text.trim()) continue;
            const frame = JSON.parse(text);
            if (match(frame)) return unwrap(frame);
          }
        }
        if (done) throw new Error("MCP stream ended without a reply.");
      }
    } finally {
      await reader.cancel();
    }
  }
  async initialize() {
    if (!this.ready)
      this.ready = (async () => {
        const result = await this.send("initialize", {
          protocolVersion: this.version,
          capabilities: {},
          clientInfo: { name: "backlog-web", version: "0.15.2" },
        });
        if (!["2025-11-25", "2025-06-18", "2025-03-26"].includes(result.protocolVersion))
          throw new Error("Unsupported MCP protocol version.");
        this.version = result.protocolVersion;
        await this.send("notifications/initialized", {}, true);
      })().catch((error) => {
        this.ready = null;
        throw error;
      });
    return this.ready;
  }
  async call(name, args = {}) {
    await this.initialize();
    let result;
    const previous = this.session;
    try {
      result = await this.send("tools/call", { name, arguments: args });
    } catch (error) {
      if (!(error instanceof HttpError) || error.status !== 404) throw error;
      // A concurrent request may already have replaced the expired session.
      if (this.session === previous) {
        this.session = null;
        this.ready = null;
      }
      await this.initialize();
      result = await this.send("tools/call", { name, arguments: args });
    }
    const text = (result.content || [])
      .filter((block) => block.type === "text")
      .map((block) => block.text)
      .join("\n");
    if (result.isError) throw new ToolError(text || "The server refused this operation.");
    if (text) return JSON.parse(text);
    if (result.structuredContent) return result.structuredContent;
    throw new Error("The tool returned no JSON data.");
  }
}
