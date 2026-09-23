import assert from "node:assert/strict";
const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch(
  process.env.BROWSER === "webkit" ? {} : { channel: "chrome" },
);
const page = await browser.newPage();
const url = process.env.UI_URL || "http://127.0.0.1:3027/ui/";
try {
  await page.goto(url);
  await page.getByRole("button", { name: "New project", exact: true }).click();
  await page.getByLabel("Name", { exact: true }).fill(`Lifecycle ${Date.now()}`);
  await page.getByRole("button", { name: "Create project", exact: true }).click();
  await page.locator(".board-page").waitFor();
  const project = new URL(page.url()).searchParams.get("project");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Archive project", exact: true }).click();
  await page.getByRole("button", { name: "Restore project", exact: true }).waitFor();
  await page.goto(url);
  assert.equal(await page.locator(".project").filter({ hasText: project }).count(), 0);
  await page.getByLabel("Include archived").check();
  await page.locator(".project").filter({ hasText: project }).click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Restore project", exact: true }).click();
  await page.getByRole("button", { name: "Archive project", exact: true }).waitFor();
  await page.getByRole("button", { name: "Delete project", exact: true }).click();
  const dialog = page.locator(".action-dialog");
  await dialog.waitFor();
  assert.match(await dialog.innerText(), /0 task\(s\) and 0 note\(s\)/);
  for (const width of [1280, 390])
    for (const colorScheme of ["light", "dark"]) {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme });
      if (process.env.SCREENSHOT_DIR)
        await page.screenshot({
          path: `${process.env.SCREENSHOT_DIR}/delete-${width}-${colorScheme}.png`,
        });
    }
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await dialog.waitFor({ state: "detached" });
  await page.reload();
  await page.getByRole("button", { name: "Delete project", exact: true }).click();
  await dialog.getByRole("button", { name: "Delete permanently", exact: true }).click();
  await page.getByRole("heading", { name: "Your projects", exact: true }).waitFor();
  assert.equal(await page.locator(".project").filter({ hasText: project }).count(), 0);
  console.log(
    "Project lifecycle: archive, archived list, restore, MCP confirmation cancel and confirmed deletion passed",
  );
} finally {
  await browser.close();
}
