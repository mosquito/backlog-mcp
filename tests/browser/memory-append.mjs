import assert from "node:assert/strict";
const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch(
  process.env.BROWSER === "webkit" ? {} : { channel: "chrome" },
);
const page = await browser.newPage();
let fail = true;
const writes = [];
const url = process.env.UI_URL || "http://127.0.0.1:3027/ui/";
try {
  await page.goto(url);
  const note = await page.evaluate(async () => {
    const { MCP } = await import("/ui/static/mcp.js");
    const client = new MCP("/mcp");
    const p = await client.call("project_save", { name: "Append test", root: "" });
    return client.call("memory_save", {
      project: p.full_name,
      title: "Appendable note",
      body: "Original text",
    });
  });
  await page.route("**/mcp", (route) => {
    const rpc = route.request().postDataJSON();
    if (rpc.params?.name === "memory_append") {
      writes.push(rpc.params.arguments);
      if (fail) {
        fail = false;
        return route.fulfill({ status: 503, body: "Try again" });
      }
    }
    return route.continue();
  });
  await page.goto(url + "#note" + note.id);
  await page.getByRole("button", { name: "Append to note", exact: true }).click();
  const dialog = page.locator(".action-dialog");
  const text = dialog.getByLabel("Addition", { exact: true });
  await text.fill("x".repeat(1024));
  await dialog.getByRole("button", { name: "Append", exact: true }).click();
  assert.equal(writes.length, 0);
  await text.fill("Additional fact");
  for (const width of [1280, 390])
    for (const colorScheme of ["light", "dark"]) {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme });
      if (process.env.SCREENSHOT_DIR)
        await page.screenshot({
          path: `${process.env.SCREENSHOT_DIR}/append-${width}-${colorScheme}.png`,
        });
    }
  await dialog.getByRole("button", { name: "Append", exact: true }).click();
  await dialog.locator(".modal-error:not([hidden])").waitFor();
  assert.equal(await text.inputValue(), "Additional fact");
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.reload();
  await page.getByRole("button", { name: "Append to note", exact: true }).click();
  assert.equal(await text.inputValue(), "Additional fact");
  await dialog.getByRole("button", { name: "Append", exact: true }).click();
  await dialog.waitFor({ state: "detached" });
  await page.getByText("Additional fact", { exact: true }).waitFor();
  assert.match(await page.locator(".memory-body").innerText(), /Original text/);
  assert.deepEqual(writes.at(-1), { id: note.id, text: "Additional fact" });
  await page.getByRole("button", { name: "Append to note", exact: true }).click();
  assert.equal(await text.inputValue(), "");
  console.log(
    "Memory append: length limit, error/reload draft, append-only request and successful draft clearing passed",
  );
} finally {
  await browser.close();
}
