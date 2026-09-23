// Run against a local UI with PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs.
import assert from "node:assert/strict";
const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch(
  process.env.BROWSER === "webkit" ? {} : { channel: "chrome" },
);
const page = await browser.newPage();
let releaseIdentity,
  releasePage,
  refuse = false;
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
try {
  await page.route("**/mcp", async (route) => {
    const body = route.request().postDataJSON();
    if (body.method !== "tools/call") return route.continue();
    const name = body.params.name;
    let result;
    if (name === "whoami") {
      if (refuse)
        return route.fulfill({
          status: 401,
          headers: { "WWW-Authenticate": 'Basic realm="test"' },
          body: "Authentication required",
        });
      await new Promise((resolve) => {
        releaseIdentity = resolve;
      });
      result = { principal: "test@browser" };
    } else if (name === "project_board") {
      await new Promise((resolve) => {
        releasePage = resolve;
      });
      result = { project: { name: "Project" }, tasks: { open: 0, done: 0, total: 0 } };
    } else if (name === "task_list") result = { items: [], count: 0 };
    else return route.continue();
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: body.id,
        result: { content: [], structuredContent: result },
      }),
    });
  });
  const url = (process.env.UI_URL || "http://127.0.0.1:3027/ui/") + "?project=test%2Fproject";
  for (const width of [1280, 390])
    for (const colorScheme of ["light", "dark"]) {
      releaseIdentity = null;
      releasePage = null;
      await page.setViewportSize({ width, height: 844 });
      await page.emulateMedia({ colorScheme });
      await page.goto(url);
      await page.locator(".initial-loading").waitFor();
      const geometry = () =>
        page.evaluate(() => ({
          header: document.querySelector("header").getBoundingClientRect().height,
          top: document.querySelector("main").getBoundingClientRect().top,
          overflow: document.documentElement.scrollWidth > innerWidth,
        }));
      const initial = await geometry();
      assert.equal(initial.overflow, false);
      assert.equal(await page.locator("header").evaluate((n) => n.inert), true);
      if (process.env.SCREENSHOT_DIR)
        await page.screenshot({
          path: `${process.env.SCREENSHOT_DIR}/cold-${width}-${colorScheme}.png`,
        });
      while (!releaseIdentity) await page.waitForTimeout(10);
      releaseIdentity();
      while (!releasePage) await page.waitForTimeout(10);
      assert.deepEqual(await geometry(), initial);
      releasePage();
      await page.locator(".board-page").waitFor();
      assert.deepEqual(await geometry(), initial);
      assert.equal(await page.locator("header").evaluate((n) => n.inert), false);
      assert.equal(await page.locator(".initial-loading").count(), 0);
      if (process.env.SCREENSHOT_DIR)
        await page.screenshot({
          path: `${process.env.SCREENSHOT_DIR}/ready-${width}-${colorScheme}.png`,
        });
    }
  refuse = true;
  await page.reload();
  await page.locator(".login").waitFor();
  assert.equal(await page.locator("header").evaluate((n) => n.inert), false);
  assert.equal(await page.locator(".initial-loading").count(), 0);
  assert.equal(await page.locator("#content").getAttribute("aria-busy"), null);
  assert.deepEqual(errors, []);
  console.log(
    "Cold start: stable header and content position through identity/data loading; login fallback passed",
  );
} finally {
  releaseIdentity?.();
  releasePage?.();
  await browser.close();
}
