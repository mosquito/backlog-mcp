function credentialKeys(endpoint) {
  const address = new URL(endpoint, location.origin).href;
  return ["/console", "/console/"].map(
    (path) => `mcp-console-credentials:${JSON.stringify([path, address])}`,
  );
}

export function authorization(record) {
  if (!record) return "";
  const { method, username, secret } = record;
  if (typeof username !== "string" || typeof secret !== "string")
    throw new Error("Enter valid authentication fields.");
  if (method === "basic") {
    if (username.includes(":") || /[\x00-\x1f\x7f]/.test(username + secret))
      throw new Error("Basic credentials cannot contain control characters or a colon in the username.");
    const bytes = new TextEncoder().encode(`${username}:${secret}`);
    return "Basic " + btoa(Array.from(bytes, (byte) => String.fromCharCode(byte)).join(""));
  }
  if (method === "bearer") {
    if (!/^[A-Za-z0-9._~+/-]+=*$/.test(secret))
      throw new Error("Enter a valid Bearer token.");
    return "Bearer " + secret;
  }
  throw new Error("Choose Basic or Bearer authentication.");
}

export function loadCredentials(endpoint) {
  const address = new URL(endpoint, location.origin).href;
  for (const key of credentialKeys(endpoint)) {
    try {
      const record = JSON.parse(localStorage.getItem(key));
      if (record?.version !== 1 || record.endpoint !== address || typeof record.header !== "string")
        continue;
      authorization(record);
      return record;
    } catch {
      // Keep manual login available when storage is unavailable or invalid.
    }
  }
  return null;
}

export function clearCredentials(endpoint) {
  for (const key of credentialKeys(endpoint)) {
    try {
      localStorage.removeItem(key);
    } catch {}
  }
  const legacyKey = `backlog:auth:${new URL(endpoint, location.origin).pathname}`;
  try {
    localStorage.removeItem(legacyKey);
  } catch {}
  try {
    sessionStorage.removeItem(legacyKey);
  } catch {}
}

export function saveCredentials(endpoint, fields) {
  const record = {
    version: 1,
    endpoint: new URL(endpoint, location.origin).href,
    method: fields.method,
    username: fields.username,
    secret: fields.secret,
    header: "",
  };
  authorization(record);
  clearCredentials(endpoint);
  try {
    localStorage.setItem(credentialKeys(endpoint)[0], JSON.stringify(record));
  } catch {}
}
