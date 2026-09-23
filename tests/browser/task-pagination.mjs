// Run with UI_URL=http://127.0.0.1:3027/ui/ PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs node tests/browser/task-pagination.mjs
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

export async function run(page, url) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const tasks = [
    ...Array.from({ length: 135 }, (_, i) => ({ id: i + 1, status: "todo" })),
    ...Array.from({ length: 3 }, (_, i) => ({ id: i + 136, status: "doing" })),
    ...Array.from({ length: 125 }, (_, i) => ({ id: i + 139, status: "done" })),
  ].map((task) => ({
    ...task,
    title: `Task ${task.id}`,
    priority: "normal",
    tags: [],
    updated_at: 1,
  }));
  const requests = [];
  let fail = false,
    hold = false,
    release;
  await page.route("**/mcp", async (route) => {
    const body = route.request().postDataJSON();
    if (body.method !== "tools/call") return route.continue();
    const { name, arguments: args } = body.params;
    let result;
    if (name === "whoami") result = { principal: "test@browser" };
    else if (name === "project_board")
      result = {
        project: { name: "Pagination" },
        tasks: { open: 138, done: 125, total: 263 },
      };
    else if (name === "task_list") {
      requests.push(args);
      if (args.offset === 50 && fail) {
        fail = false;
        return route.fulfill({ status: 503, body: "Temporary failure" });
      }
      if (args.offset === 50 && hold) {
        hold = false;
        await new Promise((resolve) => {
          release = resolve;
        });
      }
      let selected = tasks.filter((task) => args.status.includes(task.status));
      if (args.tag === "small") selected = selected.slice(0, 3);
      if (args.tag === "empty") selected = [];
      const offset = args.offset || 0;
      result = {
        items: selected.slice(offset, offset + args.limit),
        count: selected.length,
      };
    } else return route.continue();
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: body.id,
        result: { content: [], structuredContent: result },
      }),
    });
  });
  const base = new URL(url);
  const board = (view = "kanban") => `${base.origin}/ui/?project=test%2Fpagination&view=${view}`;
  const bottom = async (selector) =>
    page.locator(selector).evaluate((node) => {
      node.scrollTop = node.scrollHeight;
    });
  const count = async (selector, n) =>
    page.waitForFunction(({ selector, n }) => document.querySelectorAll(selector).length === n, {
      selector,
      n,
    });
  await page.goto(board());
  await count('.column[data-status="todo"] .card', 50);
  await count('.column[data-status="done"] .card', 50);
  await bottom('.column[data-status="todo"] .stack');
  await count('.column[data-status="todo"] .card', 100);
  assert.equal(await page.locator('.column[data-status="done"] .card').count(), 50);
  await bottom('.column[data-status="todo"] .stack');
  await count('.column[data-status="todo"] .card', 135);
  assert.equal(requests.filter((r) => r.status[0] === "todo" && r.offset === 50).length, 1);
  fail = true;
  await bottom('.column[data-status="done"] .stack');
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await count('.column[data-status="done"] .card', 100);
  await bottom('.column[data-status="done"] .stack');
  await count('.column[data-status="done"] .card', 125);
  assert.equal(
    new Set(await page.locator(".card").evaluateAll((nodes) => nodes.map((n) => n.dataset.task)))
      .size,
    263,
  );

  await page.goto(board("table"));
  await count(".task-table tbody tr", 50);
  const first = await page.locator(".task-table tbody tr").first().elementHandle();
  await bottom(".task-table-region");
  await count(".task-table tbody tr", 100);
  assert.equal(await first.evaluate((node) => node.isConnected), true);
  assert.ok(await page.locator(".task-table-region").evaluate((node) => node.scrollTop > 0));
  await bottom(".task-table-region");
  await count(".task-table tbody tr", 188);
  await bottom(".task-table-region");
  await count(".task-table tbody tr", 238);
  await bottom(".task-table-region");
  await count(".task-table tbody tr", 263);
  assert.equal(await page.locator(".task-pagination").textContent(), "263 tasks · All loaded");

  await page.goto(board("table"));
  await count(".task-table tbody tr", 50);
  hold = true;
  await bottom(".task-table-region");
  await page.waitForFunction(() => document.querySelector('.task-table-region[aria-busy="true"]'));
  await page.getByRole("textbox", { name: "Tag filter" }).fill("small");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await count(".task-table tbody tr", 6);
  release();
  await page.waitForTimeout(150);
  assert.equal(await page.locator(".task-table tbody tr").count(), 6);
  await page.getByRole("textbox", { name: "Tag filter" }).fill("empty");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await page.getByText("No tasks", { exact: true }).waitFor();
  assert.equal(await page.locator(".task-table tbody tr").count(), 0);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(board("table"));
  await count(".task-table tbody tr", 50);
  await bottom(".task-table-region");
  await count(".task-table tbody tr", 100);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.deepEqual(errors, []);
  await page.unroute("**/mcp");
  return { requests: requests.length, tasks: tasks.length, errors };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
  const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch(
    process.env.BROWSER === "webkit" ? {} : { channel: "chrome" },
  );
  try {
    console.log(
      await run(await browser.newPage(), process.env.UI_URL || "http://127.0.0.1:3027/ui/"),
    );
  } finally {
    await browser.close();
  }
}
