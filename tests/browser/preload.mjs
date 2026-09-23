// Run against a local UI with PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs.
import assert from "node:assert/strict";
import { createServer } from "node:http";
const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const isWebKit = process.env.BROWSER === "webkit";
const upstream = process.env.UI_URL || "http://127.0.0.1:3027/ui/";
const index = await fetch(upstream);
const html = await index.text();
const links = index.headers.get("link");
assert.ok(links);
const hints = [...links.matchAll(/<([^>]+)>; ([^,]+)/g)];
const eager = hints.filter(([, , attributes]) => !attributes.includes("media=")).map(([, path]) => path);
let release;
const gate = new Promise((resolve) => { release = resolve; });
let sent = false;
const requests = [];
const server = createServer(async (request, response) => {
  if (request.url === "/ui/") {
    response.writeHead(200, { "Content-Type": "text/html", Link: links });
    response.flushHeaders();
    await gate;
    sent = true;
    response.end(html);
  } else if (request.url.startsWith("/ui/static/")) {
    requests.push({ path: request.url, before: !sent });
    const asset = await fetch(new URL(request.url, upstream));
    response.writeHead(asset.status, { "Content-Type": asset.headers.get("content-type") });
    response.end(Buffer.from(await asset.arrayBuffer()));
  } else {
    response.writeHead(401);
    response.end();
  }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const browser = await (isWebKit ? webkit : chromium).launch(isWebKit ? {} : { channel: "chrome" });
try {
  const page = await browser.newPage();
  const navigation = page.goto(`http://127.0.0.1:${server.address().port}/ui/`);
  // Hold all HTML bytes to distinguish HTTP header hints from parser discovery.
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline && !eager.every((path) => requests.some((r) => r.path === path)))
    await new Promise((resolve) => setTimeout(resolve, 20));
  const early = requests.filter((r) => r.before).map((r) => r.path);
  release();
  await navigation;
  if (!isWebKit) assert.deepEqual(early.sort(), eager.sort());
  for (const path of eager) assert.equal(requests.filter((r) => r.path === path).length, 1, path);
  assert.ok(!requests.some((r) => r.path.endsWith("mermaid.min.js")));
  console.log(`${early.length} resources requested before HTML; no duplicate asset requests`);
} finally {
  release();
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
