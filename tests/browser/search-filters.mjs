import assert from "node:assert/strict";
const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch(
  process.env.BROWSER === "webkit" ? {} : { channel: "chrome" },
);
const page = await browser.newPage();
const calls = [];
let release,
  helpFailure = true;
try {
  await page.route("**/mcp", async (route) => {
    const rpc = route.request().postDataJSON();
    if (rpc.method === "tools/call" && rpc.params?.name === "guide" && helpFailure) {
      helpFailure = false;
      return route.fulfill({ status: 503, body: "Unavailable" });
    }
    if (rpc.method !== "tools/call") return route.continue();
    const { name, arguments: args } = rpc.params;
    let result;
    if (name === "whoami") result = { principal: "test@browser" };
    else if (name === "project_list") result = { items: [], count: 0 };
    else if (name === "project_board")
      result = { project: { name: "Project" }, tasks: { open: 0, done: 0, total: 0 } };
    else if (name === "task_get") result = {
      id: 123, project: "test/project", title: "History task", body: "",
      status: "todo", priority: "normal", links: [], comments: [], tags: [],
    };
    else if (name === "task_list") result = { items: [], count: 0 };
    else if (name === "search") {
      calls.push(args);
      if (args.query === "failure") return route.fulfill({ status: 503, body: "Unavailable" });
      if (args.query === "slow")
        await new Promise((resolve) => {
          release = resolve;
        });
      result = {
        tasks: args.query === "history-task" ? [{id: 123, project: "test/project", title: "History task"}] : [],
        comments: [],
        memory: [],
        projects: args.query === "positions"
          ? Array.from({ length: 40 }, (_, index) => ({
              id: index + 1, full_name: "test/project", name: `Result ${index + 1}`,
            }))
          : [{ id: 1, full_name: "test/project", name: args.query }],
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
  assert.equal(await page.locator(".search-help").getAttribute("open"), null);
  await page.locator(".search-help summary").click();
  await page
    .locator(".search-help-content")
    .getByText(/reopen query syntax to retry/)
    .waitFor();
  await page.locator(".search-help summary").click();
  await page.locator(".search-help summary").click();
  await page.locator(".search-help-content table").waitFor();
  assert.match(await page.locator(".search-help").innerText(), /A star inside quotes/);
  assert.doesNotMatch(await page.locator(".search-help").innerText(), /SQLite|FTS5/);
  if (process.env.SCREENSHOT_DIR) {
    for (const width of [1280, 390])
      for (const colorScheme of ["light", "dark"]) {
        await page.setViewportSize({ width, height: 844 });
        await page.emulateMedia({ colorScheme });
        await page.screenshot({
          path: `${process.env.SCREENSHOT_DIR}/search-help-${width}-${colorScheme}.png`,
        });
      }
  }
  await page.locator(".search-help summary").click();
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
  await page.locator("#search-query").fill("positions");
  await page.locator("#search-projects-40").waitFor();
  for (let i = 0; i < 15; i++) await page.locator("#search-query").press("ArrowDown");
  const selected = await page.locator("#search-query").getAttribute("aria-activedescendant");
  const scroll = await page.locator("#search-results").evaluate((node) => node.scrollTop);
  assert.ok(scroll > 0);
  await page.locator("#search-query").press("Escape");
  await page.locator("#search-open").click();
  await page.locator("#search-projects-40").waitFor();
  assert.equal(await page.locator("#search-query").getAttribute("aria-activedescendant"), selected);
  assert.equal(await page.locator("#search-results").evaluate((node) => node.scrollTop), scroll);
  await page.locator("#search-projects-20").click();
  await page.waitForFunction(() => !document.querySelector("#content").hasAttribute("aria-busy"));
  await page.locator("#search-open").click();
  await page.locator("#search-projects-40").waitFor();
  assert.equal(
    await page.locator("#search-query").getAttribute("aria-activedescendant"),
    "search-projects-20",
  );
  await page.locator("#search-query").fill("fresh");
  await page.getByRole("option").filter({ hasText: "fresh" }).waitFor();
  assert.equal(await page.locator("#search-query").getAttribute("aria-activedescendant"), "search-projects-1");
  assert.equal(await page.locator("#search-results").evaluate((node) => node.scrollTop), 0);
  await page.locator("#search-query").fill("history-task");
  await page.locator("#search-tasks-123").click();
  await page.waitForURL("**/#123");
  await page.locator("#search-open").click();
  await page.locator("#search-close").click();
  await page.reload();
  await page.locator("#search-open").click();
  await page.locator(".search-history summary").click();
  const history = page.locator("#search-history-list");
  assert.equal(await history.getByRole("button", {name: "positions", exact: true}).count(), 0);
  assert.equal(await history.getByRole("button", {name: "history-task", exact: true}).count(), 1);
  assert.equal(await history.getByRole("button", {name: "failure", exact: true}).count(), 0);
  await history.getByRole("button", {name: "history-task", exact: true}).click();
  await page.locator("#search-tasks-123").waitFor();
  assert.equal(await page.locator("#search-query").inputValue(), "history-task");
  await page.locator(".search-history summary").click();
  await page.locator("#search-history-clear").click();
  assert.equal(await history.getByRole("button").count(), 0);
  await page.locator("#search-close").click();
  await page.reload();
  await page.locator("#search-open").click();
  await page.locator(".search-history summary").click();
  assert.equal(await history.getByRole("button").count(), 0);
  console.log(
    "Search filters: scopes, limits, persistence, stale replies, keyboard and responsive layout passed",
  );
} finally {
  await browser.close();
}
