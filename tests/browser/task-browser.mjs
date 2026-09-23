import assert from "node:assert/strict";
const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch(
  process.env.BROWSER === "webkit" ? {} : { channel: "chrome" },
);
const page = await browser.newPage();
const calls = [];
const url = process.env.UI_URL || "http://127.0.0.1:3027/ui/";
try {
  await page.route("**/mcp", async (route) => {
    const rpc = route.request().postDataJSON();
    if (rpc.method !== "tools/call") return route.continue();
    const { name, arguments: args } = rpc.params;
    calls.push({ name, args });
    let result;
    if (name === "whoami") result = { principal: "test@browser" };
    else if (name === "project_list")
      result = {
        items: [
          {
            full_name: "test/project",
            name: "Project",
            role: "owner",
            open_tasks: 75,
            total_tasks: 75,
          },
        ],
        count: 1,
      };
    else if (name === "task_list")
      result = {
        count: 75,
        items: Array.from({ length: args.offset ? 25 : 50 }, (_, i) => ({
          id: (args.offset || 0) + i + 1,
          title: "Task " + ((args.offset || 0) + i + 1),
          project: i % 2 ? "test/project" : "test/other",
          status: "todo",
          priority: "high",
          tags: ["ui"],
        })),
      };
    else if (name === "memory_list") result = { items: [], count: 0 };
    else return route.continue();
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: rpc.id,
        result: { content: [], structuredContent: result },
      }),
    });
  });
  await page.goto(url + "?view=tasks");
  await page.locator(".task-table tbody tr").first().waitFor();
  assert.match(await page.locator(".task-table").innerText(), /test\/other/);
  await page.getByRole("combobox", { name: "Scope", exact: true }).selectOption("computer");
  await page.getByRole("textbox", { name: "Scope computer", exact: true }).fill("laptop");
  await page.getByRole("combobox", { name: "Task status", exact: true }).selectOption("todo");
  await page.getByRole("combobox", { name: "Task priority", exact: true }).selectOption("high");
  await page.getByRole("textbox", { name: "Task tag", exact: true }).fill("ui");
  await page.getByRole("combobox", { name: "Task order", exact: true }).selectOption("recent");
  await page.getByRole("button", { name: "Apply filters", exact: true }).click();
  await page.waitForFunction(() => !document.querySelector("#content").hasAttribute("aria-busy"));
  await page.locator(".task-table-region").evaluate((node) => {
    node.scrollTop = node.scrollHeight;
  });
  await page.getByText("75 of 75 tasks shown", { exact: true }).waitFor();
  const last = calls.filter((c) => c.name === "task_list").at(-1).args;
  assert.deepEqual(last, {
    computer: "laptop",
    limit: 50,
    order: "recent",
    status: ["todo"],
    priority: "high",
    tag: "ui",
    offset: 50,
  });
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
          path: `${process.env.SCREENSHOT_DIR}/tasks-${width}-${colorScheme}.png`,
        });
    }
  await page.reload();
  await page.locator(".task-table").waitFor();
  assert.equal(
    await page.getByRole("textbox", { name: "Task tag", exact: true }).inputValue(),
    "ui",
  );
  await page.goto(url + "?scope=computer&computer=laptop");
  await page.locator(".projects").waitFor();
  assert.deepEqual(calls.filter((c) => c.name === "project_list").at(-1).args, {
    computer: "laptop",
    archived: false,
  });
  await page.goto(url + "?view=memory&scope=current");
  await page.locator(".memory-page").waitFor();
  assert.equal(calls.filter((c) => c.name === "memory_list").at(-1).args.all_projects, true);
  await page.getByRole("combobox", { name: "Scope", exact: true }).selectOption("computer");
  await page.getByRole("textbox", { name: "Scope computer", exact: true }).fill("remote");
  await page.getByRole("button", { name: "Apply filters", exact: true }).click();
  await page.waitForFunction(() => !document.querySelector("#content").hasAttribute("aria-busy"));
  assert.equal(calls.filter((c) => c.name === "memory_list").at(-1).args.computer, "remote");
  assert.ok(calls.every((c) => !["project_save", "task_update"].includes(c.name)));
  console.log(
    "Task browser: project labels, filters, paginated scope, URL reload and project/memory computer filters passed",
  );
} finally {
  await browser.close();
}
