import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";
const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch(
  process.env.BROWSER === "webkit" ? {} : { channel: "chrome" },
);
const page = await browser.newPage();
const bindingPath = await mkdtemp(join(tmpdir(), "backlog-ui-binding-"));
try {
  await page.goto(process.env.UI_URL || "http://127.0.0.1:3027/ui/");
  await page.getByRole("button", { name: "New project", exact: true }).click();
  await page.getByLabel("Name", { exact: true }).fill("Editable project");
  await page.getByLabel("Preferred language", { exact: true }).fill("Russian");
  await page.getByRole("button", { name: "Create project", exact: true }).click();
  await page.locator(".board-page").waitFor();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Edit project", exact: true }).click();
  await page.getByLabel("Name", { exact: true }).fill("Renamed project");
  await page.getByLabel("Description", { exact: true }).fill("Changed description");
  await page.getByLabel("Preferred language", { exact: true }).fill("");
  for (const width of [1280, 390])
    for (const colorScheme of ["light", "dark"]) {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme });
      if (process.env.SCREENSHOT_DIR)
        await page.screenshot({
          path: `${process.env.SCREENSHOT_DIR}/edit-${width}-${colorScheme}.png`,
        });
    }
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await page.locator(".action-dialog").waitFor({ state: "detached" });
  await page.reload();
  await page.getByRole("heading", { name: "Renamed project", exact: true }).waitFor();
  assert.match(
    await page.locator(".project-settings").innerText(),
    /Preferred language: No preference/,
  );
  const count = await page.locator(".binding-row").count();
  await page.getByRole("button", { name: "Add binding", exact: true }).click();
  await page.getByLabel("Directory", { exact: true }).fill("/does-not-exist-backlog-ui-test");
  await page
    .locator(".action-dialog")
    .getByRole("button", { name: "Add binding", exact: true })
    .click();
  await page.locator(".modal-error:not([hidden])").waitFor();
  assert.equal(
    await page.getByLabel("Directory", { exact: true }).inputValue(),
    "/does-not-exist-backlog-ui-test",
  );
  await page.getByLabel("Directory", { exact: true }).fill(bindingPath);
  await page
    .locator(".action-dialog")
    .getByRole("button", { name: "Add binding", exact: true })
    .click();
  await page.locator(".action-dialog").waitFor({ state: "detached" });
  await page.locator(".binding-row").filter({ hasText: bindingPath }).waitFor();
  assert.equal(await page.locator(".binding-row").count(), count + 1);
  console.log(
    "Project editing: metadata and language clearing persist; invalid binding retains input; existing bindings kept",
  );
} finally {
  await browser.close();
  await rm(bindingPath, {recursive:true, force:true});
}
