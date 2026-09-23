import assert from "node:assert/strict";
const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch(
  process.env.BROWSER === "webkit" ? {} : { channel: "chrome" },
);
const page = await browser.newPage();
const calls = [];
let empty = false,
  fail = false;
try {
  await page.route("**/mcp", async (route) => {
    const rpc = route.request().postDataJSON();
    if (rpc.method !== "tools/call") return route.continue();
    const { name, arguments: args } = rpc.params;
    let result;
    if (name === "whoami") result = { principal: "test@browser" };
    else if (name === "project_list") result = { items: [{ full_name: "test/project" }], count: 1 };
    else if (name === "stats") {
      calls.push(args);
      if (fail) {
        fail = false;
        return route.fulfill({ status: 503, body: "Try again" });
      }
      result = {
        total: empty ? 0 : 120,
        open: empty ? 0 : 70,
        closed: empty ? 0 : 45,
        memory: empty ? 0 : 8,
        by_status: empty ? {} : { todo: 70, done: 45, deleted: 5 },
        by_priority: empty ? {} : { normal: 100, high: 20 },
        tags: empty ? [] : ["ui", "backend"],
      };
    } else return route.continue();
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: rpc.id,
        result: { content: [], structuredContent: result },
      }),
    });
  });
  await page.goto((process.env.UI_URL || "http://127.0.0.1:3027/ui/") + "?view=stats");
  await page.locator(".stats-totals").waitFor();
  assert.deepEqual(calls.at(-1), { all_computers: true });
  assert.match(await page.locator(".stats-totals").innerText(), /120/);
  for (const width of [1280, 390])
    for (const colorScheme of ["light", "dark"]) {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme });
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
      );
      if (process.env.SCREENSHOT_DIR)
        await page.screenshot({
          path: `${process.env.SCREENSHOT_DIR}/stats-${width}-${colorScheme}.png`,
          fullPage: true,
        });
    }
  async function apply(mode, expected) {
    await page.getByRole("combobox", { name: "Scope", exact: true }).selectOption(mode);
    if (mode === "computer")
      await page.getByRole("textbox", { name: "Scope computer", exact: true }).fill("laptop");
    if (mode === "project")
      await page
        .getByRole("combobox", { name: "Scope project", exact: true })
        .selectOption("test/project");
    await page.getByRole("button", { name: "Apply scope", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector("#content").hasAttribute("aria-busy"));
    assert.deepEqual(calls.at(-1), expected);
  }
  await apply("current", { all_projects: true });
  await apply("computer", { computer: "laptop" });
  await apply("project", { project: "test/project" });
  const tagLink = page.getByRole("link", { name: "ui", exact: true });
  const tagURL = new URL(await tagLink.getAttribute("href"), page.url());
  assert.equal(tagURL.searchParams.get("view"), "tasks");
  assert.equal(tagURL.searchParams.get("tag"), "ui");
  assert.equal(tagURL.searchParams.get("project"), "test/project");
  assert.equal(tagURL.searchParams.get("status"), "all");
  empty = true;
  await apply("all", { all_computers: true });
  assert.equal(await page.locator(".stats-totals strong").first().innerText(), "0");
  fail = true;
  await page.reload();
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await page.locator(".stats-totals").waitFor();
  console.log(
    "Statistics: complete counts, four scopes, empty state, error/retry and responsive layout passed",
  );
} finally {
  await browser.close();
}
