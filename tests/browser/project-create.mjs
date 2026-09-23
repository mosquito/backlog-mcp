import assert from "node:assert/strict";
const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch(
  process.env.BROWSER === "webkit" ? {} : { channel: "chrome" },
);
const page = await browser.newPage();
let failed = false;
const writes = [];
try {
  await page.route("**/mcp", async (route) => {
    const rpc = route.request().postDataJSON();
    if (rpc.params?.name !== "project_save") return route.continue();
    writes.push(rpc.params.arguments);
    if (!failed) {
      failed = true;
      return route.fulfill({ status: 503, body: "Temporary failure" });
    }
    return route.continue();
  });
  await page.goto(process.env.UI_URL || "http://127.0.0.1:3027/ui/");
  await page.getByRole("button", { name: "New project", exact: true }).click();
  const dialog = page.locator(".action-dialog");
  await dialog.getByLabel("Name", { exact: true }).fill("Browser project");
  await dialog.getByLabel("Description", { exact: true }).fill("Created from the UI");
  await dialog.getByLabel("Preferred language", { exact: true }).fill("Russian");
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
          path: `${process.env.SCREENSHOT_DIR}/create-${width}-${colorScheme}.png`,
        });
    }
  await dialog.getByRole("button", { name: "Create project", exact: true }).click();
  await dialog.locator(".modal-error:not([hidden])").waitFor();
  assert.equal(await dialog.getByLabel("Name", { exact: true }).inputValue(), "Browser project");
  await dialog.getByRole("button", { name: "Create project", exact: true }).click();
  await page.locator(".board-page").waitFor();
  assert.equal(writes.length, 2);
  assert.deepEqual(writes[1], {
    root: "",
    name: "Browser project",
    description: "Created from the UI",
    preferred_language: "Russian",
  });
  assert.ok(new URL(page.url()).searchParams.get("project"));
  console.log(
    "Create project: error retains fields, explicit empty root, metadata and navigation passed",
  );
} finally {
  await browser.close();
}
