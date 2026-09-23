import assert from "node:assert/strict";
const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch(
  process.env.BROWSER === "webkit" ? {} : { channel: "chrome" },
);
const page = await browser.newPage();
const url = process.env.UI_URL || "http://127.0.0.1:3027/ui/";
let fail = true;
try {
  await page.goto(url);
  const note = await page.evaluate(async () => {
    const { MCP } = await import("/ui/static/mcp.js");
    const client = new MCP("/mcp");
    const project = await client.call("project_save", { name: "Notes test", root: "" });
    return client.call("memory_save", {
      project: project.full_name,
      title: "Disposable note",
      body: "Delete only after confirmation",
    });
  });
  await page.route("**/mcp", (route) => {
    const rpc = route.request().postDataJSON();
    if (rpc.params?.name === "memory_delete" && fail) {
      fail = false;
      return route.fulfill({ status: 503, body: "Try again" });
    }
    return route.continue();
  });
  await page.goto(url + "#note" + note.id);
  await page.getByRole("button", { name: "Delete note", exact: true }).click();
  const dialog = page.locator(".action-dialog");
  assert.match(await dialog.innerText(), new RegExp(note.project));
  for (const width of [1280, 390])
    for (const colorScheme of ["light", "dark"]) {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme });
      if (process.env.SCREENSHOT_DIR)
        await page.screenshot({
          path: `${process.env.SCREENSHOT_DIR}/note-delete-${width}-${colorScheme}.png`,
        });
    }
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  assert.equal(fail, true);
  await page.getByRole("button", { name: "Delete note", exact: true }).click();
  await dialog.getByRole("button", { name: "Delete note", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "HTTP 503" }).waitFor();
  assert.match(await page.locator(".memory-body").innerText(), /Delete only after/);
  await page.getByRole("button", { name: "Delete note", exact: true }).click();
  await dialog.getByRole("button", { name: "Delete note", exact: true }).click();
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.reload();
  await page.getByRole("button", { name: "Delete note", exact: true }).click();
  await dialog.getByRole("button", { name: "Delete note", exact: true }).click();
  await dialog.getByRole("button", { name: "Delete permanently", exact: true }).click();
  await page.waitForURL((url) => !url.hash && url.searchParams.get("view") === "memory");
  await page.locator(`.memory-item[href$="#note${note.id}"]`).waitFor({ state: "detached" });
  console.log(
    "Note deletion: local cancel, error retains note, MCP cancel and confirmed deletion passed",
  );
} finally {
  await browser.close();
}
