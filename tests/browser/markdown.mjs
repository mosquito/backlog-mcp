// Run against a local server: PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs node tests/browser/markdown.mjs
import assert from "node:assert/strict";
const { webkit, chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await (process.env.BROWSER === "chromium" ? chromium : webkit).launch();
const page = await browser.newPage({
  viewport: { width: 1100, height: 1000 },
  colorScheme: "light",
});
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
try {
  await page.goto(process.env.UI_URL || "http://127.0.0.1:3019/ui/");
  await page.locator("#content form, #content .projects, #logout:not([hidden])").first().waitFor();
  const source = [
    "## Code and diagrams",
    '```python\ndef greet(name: str):\n    return f"Hello, {name}"\n```',
    '```json\n{"enabled": true, "count": 42}\n```',
    "```unknown-language\n<script>window.markdownInjected = true</script>\n```",
    "```mermaid\nflowchart LR\n A[Write] --> B{Review}\n B -->|Approve| C[Save]\n B -->|Revise| A\n```",
    "```mermaid\nsequenceDiagram\n participant User\n participant Server\n User->>Server: Save note\n Server-->>User: Saved\n```",
    "```mermaid\nthis is invalid syntax\n```",
    '<img src="data:," onerror="window.markdownInjected = true">',
    "See #42 and #note172. `#note172`",
  ].join("\n\n");
  await page.evaluate(async (source) => {
    const { markdown } = await import("/ui/static/markdown.js");
    const host = document.createElement("article");
    host.className = "markdown panel";
    host.id = "markdown-check";
    host.append(markdown(source));
    document.querySelector("#content").replaceChildren(host);
  }, source);
  await page.waitForFunction(
    () => document.querySelectorAll("backlog-diagram:not([aria-busy])").length === 3,
  );
  assert.ok((await page.locator(".hljs-keyword").count()) > 0);
  assert.equal(await page.locator(".diagram-image").count(), 2);
  assert.equal(await page.locator("backlog-diagram .error").count(), 1);
  assert.equal(await page.locator("backlog-diagram details[open]").count(), 1);
  assert.equal(
    await page.locator("code.language-unknown-language").textContent(),
    "<script>window.markdownInjected = true</script>\n",
  );
  assert.equal(await page.evaluate(() => Boolean(window.markdownInjected)), false);
  assert.equal(await page.locator("#markdown-check [onerror], #markdown-check script").count(), 0);
  assert.equal(await page.locator('a[href="/ui/#note172"]').count(), 1);
  const light = await page.locator(".diagram-image").first().getAttribute("src");
  await page.emulateMedia({ colorScheme: "dark" });
  await page.waitForFunction((light) => {
    const image = document.querySelector(".diagram-image");
    return image && image.src !== light;
  }, light);
  await page.waitForFunction(
    () => document.querySelectorAll("backlog-diagram:not([aria-busy])").length === 3,
  );
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.ok(
    await page
      .locator(".diagram-output")
      .first()
      .evaluate((node) => node.scrollWidth > node.clientWidth),
  );
  assert.ok(
    await page
      .locator(".diagram-image")
      .evaluateAll((xs) => xs.every((x) => x.complete && x.naturalWidth > 0)),
  );
  assert.deepEqual(errors, []);
  if (process.env.SCREENSHOT)
    await page.screenshot({ path: process.env.SCREENSHOT, fullPage: true });
  await page.evaluate(async () => {
    const { markdown } = await import("/ui/static/markdown.js");
    document
      .querySelector("#markdown-check")
      .replaceChildren(
        markdown(
          [
            "```mermaid",
            '%%{init: {"securityLevel":"loose","htmlLabels":true,"themeCSS":"body{display:none}"}}%%',
            'flowchart LR\n A["<img src=x onerror=window.markdownInjected=true>"] --> B[Safe]',
            'click B "javascript:window.markdownInjected=true"',
            "```",
          ].join("\n"),
        ),
      );
  });
  await page.locator("backlog-diagram:not([aria-busy])").waitFor();
  assert.equal(await page.evaluate(() => Boolean(window.markdownInjected)), false);
  assert.equal(
    await page
      .locator("#markdown-check svg, #markdown-check iframe, #markdown-check [onerror]")
      .count(),
    0,
  );
  assert.notEqual(await page.evaluate(() => getComputedStyle(document.body).display), "none");
  assert.equal(await page.evaluate(() => mermaid.mermaidAPI.getConfig().securityLevel), "strict");
  console.log(
    "Markdown: highlighting, diagrams, invalid source, sanitization, theme changes and mobile layout passed",
  );
} finally {
  await browser.close();
}
