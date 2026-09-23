import assert from "node:assert/strict";
const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch(
  process.env.BROWSER === "webkit" ? {} : { channel: "chrome" },
);
const page = await browser.newPage();
const calls = [];
let release;
try {
  await page.route("**/mcp", async (route) => {
    const rpc = route.request().postDataJSON();
    if (rpc.method !== "tools/call") return route.continue();
    const { name, arguments: args } = rpc.params;
    let result;
    if (name === "whoami") result = { principal: "test@browser" };
    else if (name === "project_list") result = { items: [], count: 0 };
    else if (name === "project_board")
      result = { project: { name: "Project" }, tasks: { open: 0, done: 0, total: 0 } };
    else if (name === "task_list") result = { items: [], count: 0 };
    else if (name === "search") {
      calls.push(args);
      if (args.query === "failure") return route.fulfill({ status: 503, body: "Unavailable" });
      if (args.query === "slow")
        await new Promise((resolve) => {
          release = resolve;
        });
      result = {
        tasks: [],
        comments: [],
        memory: [],
        projects: [{ id: 1, full_name: "test/project", name: args.query }],
      };
    } else return route.continue();
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: rpc.id,
        result: { content: [], structuredContent: result },
      }),
    });
  });
  const base = process.env.UI_URL || "http://127.0.0.1:3027/ui/";
  await page.goto(base);
  await page.locator("#search-open").click();
  await page.locator("#search-query").fill("needle");
  await page.getByRole("option").filter({ hasText: "needle" }).waitFor();
  assert.equal(calls.at(-1).limit, 15);
  assert.equal(calls.at(-1).min_score, 0.12);
  assert.equal(calls.at(-1).all_computers, true);
  await page.locator(".search-advanced summary").click();
  async function change(id, value, select = false) {
    const control = page.locator(`#search-${id}`);
    if (select) await control.selectOption(value);
    else {
      await control.fill(value);
      await control.press("Tab");
    }
    await page.waitForFunction(
      () => document.querySelector("#search-query").getAttribute("aria-busy") === "false",
    );
  }
  await change("status", "done", true);
  await change("tag", "ui");
  await change("limit", "42");
  await change("score", "0.3");
  await change("scope", "current", true);
  assert.deepEqual(calls.at(-1), {
    query: "needle",
    kinds: ["tasks", "comments", "memory", "projects"],
    limit: 42,
    min_score: 0.3,
    status: ["done"],
    tag: "ui",
    all_projects: true,
  });
  await change("scope", "computer", true);
  await change("computer", "laptop");
  assert.equal(calls.at(-1).computer, "laptop");
  for (const [id, invalid, valid] of [
    ["limit", "201", "42"],
    ["score", "2", "0.3"],
  ]) {
    const count = calls.length;
    await change(id, invalid);
    assert.equal(calls.length, count);
    assert.match(await page.locator("#search-results").innerText(), /Check the filters/);
    await change(id, valid);
  }
  for (const width of [1280, 390])
    for (const colorScheme of ["light", "dark"]) {
      await page.setViewportSize({ width, height: 844 });
      await page.emulateMedia({ colorScheme });
      assert.equal(
        await page.locator("#search-dialog").evaluate((n) => n.scrollWidth > n.clientWidth),
        false,
      );
      if (process.env.SCREENSHOT_DIR)
        await page.screenshot({
          path: `${process.env.SCREENSHOT_DIR}/search-${width}-${colorScheme}.png`,
        });
    }
  await page.locator('[name="search-kind"][value="comments"]').uncheck();
  await page.locator("#search-query").fill("failure");
  await page.getByText(/Search failed:/).waitFor();
  assert.equal(await page.locator("#search-tag").inputValue(), "ui");
  await page.locator("#search-close").click();
  await page.reload();
  await page.locator("#search-open").click();
  assert.equal(await page.locator('[name="search-kind"][value="comments"]').isChecked(), false);
  assert.equal(await page.locator("#search-tag").inputValue(), "ui");
  assert.equal(await page.locator("#search-computer").inputValue(), "laptop");
  await page.locator("#search-query").fill("slow");
  await page.waitForFunction(
    () => document.querySelector("#search-query").getAttribute("aria-busy") === "true",
  );
  while (!release) await new Promise((resolve) => setTimeout(resolve, 20));
  await page.locator("#search-query").fill("fresh");
  await page.getByRole("option").filter({ hasText: "fresh" }).waitFor();
  release();
  await page.waitForTimeout(100);
  assert.match(await page.locator("#search-results").innerText(), /fresh/);
  assert.doesNotMatch(await page.locator("#search-results").innerText(), /slow/);
  await page.locator("#search-query").press("ArrowDown");
  await page.locator("#search-query").press("Enter");
  await page.waitForURL("**/*project=test%2Fproject*");
  await page.waitForFunction(() => !document.querySelector("#content").hasAttribute("aria-busy"));
  await page.locator("#search-open").click();
  await page.locator("#search-all").uncheck();
  await page.waitForFunction(
    () => document.querySelector("#search-query").getAttribute("aria-busy") === "false",
  );
  assert.equal(calls.at(-1).project, "test/project");
  assert.equal(calls.at(-1).computer, undefined);
  assert.equal(await page.locator("#search-scope").isDisabled(), true);
  console.log(
    "Search filters: scopes, limits, persistence, stale replies, keyboard and responsive layout passed",
  );
} finally {
  await browser.close();
}
