import assert from "node:assert/strict";
const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch(
  process.env.BROWSER === "webkit" ? {} : { executablePath: process.env.CHROMIUM_EXECUTABLE, channel: process.env.CHROMIUM_EXECUTABLE ? undefined : "chrome" },
);
const page = await browser.newPage();
const url = process.env.UI_URL || "http://127.0.0.1:3027/ui/";
const writes = [];
const errors = [];
let fail = true;
page.on("pageerror", (error) => errors.push(String(error)));
try {
  await page.goto(url);
  const [project, other] = await page.evaluate(async () => {
    const { MCP } = await import("/ui/static/mcp.js");
    const client = new MCP("/mcp");
    return Promise.all([
      client.call("project_save", { name: "Memory visibility", root: "" }),
      client.call("project_save", { name: "Other project", root: "" }),
    ]);
  });
  await page.route("**/mcp*", (route) => {
    const rpc = route.request().postDataJSON();
    if (["memory_save", "memory_sharing"].includes(rpc?.params?.name)) writes.push(rpc.params);
    if (rpc?.params?.name === "memory_sharing" && fail) {
      fail = false;
      return route.fulfill({ status: 503, body: "Try again" });
    }
    return route.continue();
  });
  const params = new URLSearchParams({ view: "memory", create: "true", project: project.full_name });
  await page.goto(url + "?" + params);
  await page.getByLabel("Title", { exact: true }).fill("team dev servers catalog");
  await page.getByLabel("Content", { exact: true }).fill("Development hosts available to the team.");
  await page.getByLabel("Share with project members", { exact: true }).check();
  await page.getByLabel("Available in all projects to the same people", { exact: true }).check();
  await page.getByRole("button", { name: "Add note", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "HTTP 503" }).waitFor();
  assert.equal("shared" in writes[0].arguments, false);
  assert.equal("global_scope" in writes[0].arguments, false);
  assert.equal(writes[1].name, "memory_sharing");
  const id = writes[1].arguments.id;
  await page.reload();
  assert.equal(await page.getByLabel("Share with project members", { exact: true }).isChecked(), true);
  assert.equal(await page.getByLabel("Available in all projects to the same people", { exact: true }).isChecked(), true);
  for (const width of [1280, 390])
    for (const colorScheme of ["light", "dark"]) {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme });
      if (process.env.SCREENSHOT_DIR)
        await page.screenshot({ path: `${process.env.SCREENSHOT_DIR}/memory-visibility-${width}-${colorScheme}.png`, fullPage: true });
    }
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await page.waitForURL((url) => url.hash === "#note" + id && !url.searchParams.has("create"));
  assert.equal(writes.filter((write) => write.name === "memory_save").length, 2);
  assert.equal(writes.filter((write) => write.name === "memory_save")[1].arguments.id, id);
  assert.deepEqual(writes.at(-1).arguments, { id, shared: true, global_scope: true });
  const otherParams = new URLSearchParams({ view: "memory", project: other.full_name });
  await page.goto(url + "?" + otherParams);
  await page.locator(".memory-item").filter({ hasText: "team dev servers catalog" }).waitFor();
  params.delete("create");
  params.set("edit", "true");
  await page.goto(url + "?" + params + "#note" + id);
  await page.getByLabel("Share with project members", { exact: true }).uncheck();
  await page.getByLabel("Available in all projects to the same people", { exact: true }).uncheck();
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await page.waitForURL((url) => !url.searchParams.has("edit"));
  assert.deepEqual(writes.at(-1).arguments, { id, shared: false, global_scope: false });
  await page.goto(url + "?" + otherParams);
  await page.getByText("No notes found", { exact: true }).waitFor();
  assert.deepEqual(errors, []);
  console.log("Memory visibility: shared/global tool, failed save, restored draft, retry and project scope passed");
} finally {
  await browser.close();
}
