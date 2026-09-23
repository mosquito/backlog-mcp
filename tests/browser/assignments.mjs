import assert from "node:assert/strict";
const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch(
  process.env.BROWSER === "webkit" ? {} : { channel: "chrome" },
);
const page = await browser.newPage();
let assigned = null,
  role = "editor",
  conflict = true;
const writes = [];
const reads = [];
const task = {
  id: 1,
  project: "test/project",
  title: "Assignable task",
  body: "",
  status: "todo",
  priority: "normal",
  links: [],
  comments: [],
  tags: [],
};
try {
  await page.route("**/mcp", async (route) => {
    const rpc = route.request().postDataJSON();
    if (rpc.method !== "tools/call") return route.continue();
    const { name, arguments: args } = rpc.params;
    let result;
    if (name === "whoami") result = { principal: "test@browser" };
    else if (name === "project_board") result = { project: { role }, tasks: {} };
    else if (name === "task_get") result = { ...task, assigned };
    else if (name === "task_claim") {
      writes.push(args);
      if (conflict) {
        conflict = false;
        return route.fulfill({
          contentType: "application/json",
          body: JSON.stringify({
            jsonrpc: "2.0",
            id: rpc.id,
            result: { isError: true, content: [{ type: "text", text: "Assignment conflict" }] },
          }),
        });
      }
      assert.equal(args.expected, assigned);
      assigned = args.assigned;
      result = { ...task, assigned };
    } else if (name === "task_assigned") {
      reads.push(args);
      result = {
        count: 51,
        items: Array.from({ length: args.offset ? 1 : 50 }, (_, i) => ({
          ...task,
          id: args.offset + i + 1,
          assigned: "test@browser:agent",
        })),
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
  const url = process.env.UI_URL || "http://127.0.0.1:3027/ui/";
  await page.goto(url + "#1");
  await page.getByRole("button", { name: "Claim task", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "Assignment conflict" }).waitFor();
  assert.equal(assigned, null);
  await page.getByRole("button", { name: "Claim task", exact: true }).click();
  await page.getByRole("button", { name: "Release task", exact: true }).waitFor();
  assert.match(assigned, /^test@browser:browser-[a-z0-9-]+$/);
  const previous = assigned;
  await page.reload();
  await page.getByRole("button", { name: "Release task", exact: true }).click();
  await page.getByRole("button", { name: "Claim task", exact: true }).waitFor();
  assert.equal(writes.at(-1).expected, previous);
  assert.equal(assigned, null);
  await page.getByRole("button", { name: "Claim task", exact: true }).click();
  await page.getByRole("button", { name: "Release task", exact: true }).waitFor();
  assert.equal(assigned, previous);
  role = "reader";
  await page.reload();
  await page.locator(".detail").waitFor();
  assert.equal(await page.getByRole("button", { name: "Release task", exact: true }).count(), 0);
  await page.goto(url + "?view=assigned");
  await page.getByRole("link", { name: "Next", exact: true }).click();
  await page.getByText("51 assignments · 51–51 shown").waitFor();
  assert.equal(reads.at(-1).offset, 50);
  await page.getByLabel("Assignment prefix").fill("other@computer:");
  await page.getByRole("button", { name: "Filter", exact: true }).click();
  await page.getByText("51 assignments · 1–50 shown").waitFor();
  assert.equal(reads.at(-1).prefix, "other@computer:");
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
          path: `${process.env.SCREENSHOT_DIR}/assignments-${width}-${colorScheme}.png`,
        });
    }
  console.log(
    "Assignments: conflict, claim, release, persistent marker, reader, pagination and prefix passed",
  );
} finally {
  await browser.close();
}
