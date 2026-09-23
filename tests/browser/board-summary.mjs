import assert from "node:assert/strict";
const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch(
  process.env.BROWSER === "webkit" ? {} : { channel: "chrome" },
);
const page = await browser.newPage();
let empty = false;
try {
  await page.route("**/mcp", async (route) => {
    const rpc = route.request().postDataJSON();
    if (rpc.method !== "tools/call") return route.continue();
    let result;
    if (rpc.params.name === "whoami") result = { principal: "test@browser" };
    else if (rpc.params.name === "project_board")
      result = {
        project: { name: "Project" },
        tasks: { open: 12, done: 0, total: 12 },
        ready: {
          count: empty ? 0 : 12,
          items: empty ? [] : [{ id: 1, title: "Ready task", status: "todo", priority: "normal" }],
        },
        claimed: { count: 0, items: [] },
      };
    else if (rpc.params.name === "task_list") result = { items: [], count: 0 };
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
  await page.goto((process.env.UI_URL || "http://127.0.0.1:3027/ui/") + "?project=test%2Fproject");
  await page.getByText("Ready (12)", { exact: true }).click();
  await page.getByText("My active tasks (0)", { exact: true }).click();
  assert.match(await page.locator(".board-summaries").innerText(), /1 of 12 shown/);
  assert.equal(
    await page.getByRole("link", { name: "#1 Ready task" }).getAttribute("href"),
    "/ui/#1",
  );
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
          path: `${process.env.SCREENSHOT_DIR}/summary-${width}-${colorScheme}.png`,
        });
    }
  empty = true;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await page.getByText("Ready (0)", { exact: true }).waitFor();
  console.log("Board sections: counts, truncated list, links, empty state and refresh passed");
} finally {
  await browser.close();
}
