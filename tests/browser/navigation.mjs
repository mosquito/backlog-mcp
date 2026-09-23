// Run against a local UI with PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs.
import assert from "node:assert/strict";
const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch(
  process.env.BROWSER === "webkit" ? {} : { channel: "chrome" },
);
const page = await browser.newPage();
const base = process.env.UI_URL || "http://127.0.0.1:3027/ui/";
let pending = [],
  slow = false,
  fail = false;
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
try {
  await page.route("**/mcp", async (route) => {
    const body = route.request().postDataJSON();
    if (body.method !== "tools/call") return route.continue();
    const { name } = body.params;
    if (slow && name === "task_list") await new Promise((resolve) => pending.push(resolve));
    if (fail && name === "task_list")
      return route.fulfill({ status: 503, body: "Temporary failure" });
    const results = {
      whoami: { principal: "test@browser" },
      task_get: {
        id: 901,
        project: "test/project",
        title: "Long task",
        body: "Paragraph.\n\n".repeat(100),
        status: "todo",
        priority: "normal",
        tags: [],
        links: [],
        comments: [],
        created_at: 1,
        updated_at: 1,
      },
      project_board: {
        project: { name: "Project" },
        tasks: { open: 0, done: 0, total: 0, deleted: 0 },
      },
      task_list: { items: [], count: 0 },
      project_list: { items: [], count: 0 },
    };
    if (!(name in results)) return route.continue();
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: body.id,
        result: { content: [], structuredContent: results[name] },
      }),
    });
  });
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 844 });
    for (const colorScheme of ["light", "dark"]) {
      await page.emulateMedia({ colorScheme });
      await page.goto(`${base}#901`);
      await page.locator(".detail").waitFor();
      await page.evaluate(() => window.scrollTo(0, 500));
      const geometry = () =>
        page.evaluate(() => ({
          height: document.querySelector("#content").getBoundingClientRect().height,
          header: document.querySelector("header").getBoundingClientRect().height,
          y: scrollY,
          width: document.documentElement.clientWidth,
        }));
      const before = await geometry();
      slow = true;
      const crumb = await page
        .locator("#breadcrumbs")
        .getByRole("link", { name: "test/project", exact: true })
        .boundingBox();
      await page.mouse.click(crumb.x + crumb.width / 2, crumb.y + crumb.height / 2);
      await page.locator("#page-loading:not([hidden])").waitFor();
      assert.deepEqual(await geometry(), before);
      assert.equal(await page.locator("#content").evaluate((n) => n.inert), true);
      assert.equal(await page.locator(".detail").count(), 1);
      if (process.env.SCREENSHOT_DIR)
        await page.screenshot({
          path: `${process.env.SCREENSHOT_DIR}/navigation-${width}-${colorScheme}.png`,
        });
      // Navigate again through the header before the first request completes.
      await page
        .locator("#breadcrumbs")
        .getByRole("link", { name: "Projects", exact: true })
        .click();
      await page.getByRole("heading", { name: "Your projects" }).waitFor();
      slow = false;
      pending.splice(0).forEach((resolve) => resolve());
      await page.waitForTimeout(100);
      assert.equal(await page.getByRole("heading", { name: "Your projects" }).count(), 1);
      assert.equal(await page.locator("#content").evaluate((n) => n.inert), false);
      assert.equal(await page.locator("#page-loading").isVisible(), false);
    }
  }
  fail = true;
  await page.goto(`${base}?project=test%2Fproject`);
  await page.getByRole("heading", { name: "Could not load this page" }).waitFor();
  assert.equal(await page.locator("#content").evaluate((n) => n.inert), false);
  fail = false;
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await page.locator(".board-page").waitFor();
  assert.equal(await page.locator("#page-loading").isVisible(), false);
  assert.deepEqual(errors, []);
  console.log("Stable pending geometry, rapid navigation, stale responses, error/retry: passed");
} finally {
  pending.forEach((resolve) => resolve());
  await browser.close();
}
