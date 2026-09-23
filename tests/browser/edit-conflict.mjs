import assert from "node:assert/strict";
const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch(
  process.env.BROWSER === "webkit" ? {} : { channel: "chrome" },
);
const context = await browser.newContext();
const page = await context.newPage();
const base = process.env.UI_URL || "http://127.0.0.1:3027/ui/";
async function api(name, args) {
  return page.evaluate(
    async ({ name, args }) => {
      const { MCP } = await import("/ui/static/mcp.js");
      const client = new MCP("/mcp");
      try {
        return await client.call(name, args);
      } finally {
        client.close();
      }
    },
    { name, args },
  );
}
try {
  await page.goto(base);
  await page.locator("#search-open").waitFor();
  const principal = (await api("whoami", {})).principal;
  const project = await api("project_save", { name: `Edit test ${Date.now()}`, root: "" });
  const task = await api("task_add", {
    project: project.full_name,
    title: "Concurrent editing",
    body: "Original **Markdown**",
  });
  const original = `${principal}:original-assignee`;
  await api("task_claim", { task: task.id, expected: null, assigned: original });
  await page.goto(`${base}?view=edit#${task.id}`);
  await page.getByRole("button", { name: "Save changes", exact: true }).waitFor();
  await page.waitForFunction(() => !document.querySelector(".editor button[type=submit]").disabled);
  const first = (await api("task_get", { id: task.id })).assigned;
  assert.match(first, /:ui-edit-/);
  await page
    .getByRole("textbox", { name: "Description", exact: true })
    .fill("My **draft** survives");
  await page.reload();
  await page.waitForFunction(
    () =>
      document.querySelector(".editor button[type=submit]") &&
      !document.querySelector(".editor button[type=submit]").disabled,
  );
  assert.equal(
    await page.getByRole("textbox", { name: "Description", exact: true }).inputValue(),
    "My **draft** survives",
  );
  assert.equal((await api("task_get", { id: task.id })).assigned, first);
  const second = await context.newPage();
  await second.goto(`${base}?view=edit#${task.id}`);
  await second.getByRole("heading", { name: "Resolve editing conflict" }).waitFor();
  assert.equal((await api("task_get", { id: task.id })).assigned, first);
  await second.close();
  let race = true;
  await page.route("**/mcp", async (route) => {
    const rpc = route.request().postDataJSON();
    if (race && rpc.method === "tools/call" && rpc.params.name === "task_update") {
      race = false;
      await api("task_claim", { task: task.id, expected: first, assigned: null });
      await api("task_update", { id: task.id, body: "Server **changed**\n\nAnother paragraph." });
    }
    await route.continue();
  });
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await page.getByRole("heading", { name: "Resolve editing conflict" }).waitFor();
  assert.equal(
    await page.getByRole("textbox", { name: "Your Markdown", exact: true }).inputValue(),
    "My **draft** survives",
  );
  assert.match(
    await page.getByRole("textbox", { name: "Server Markdown", exact: true }).inputValue(),
    /Server \*\*changed\*\*/,
  );
  assert.match((await api("task_get", { id: task.id })).body, /Server \*\*changed\*\*/);
  for (const width of [1280, 390])
    for (const colorScheme of ["light", "dark"]) {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme });
      await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
      );
      if (process.env.SCREENSHOT_DIR)
        await page.screenshot({
          path: `${process.env.SCREENSHOT_DIR}/conflict-${width}-${colorScheme}.png`,
          fullPage: true,
        });
    }
  await page.getByRole("button", { name: "Compare preview", exact: true }).click();
  assert.match(await page.locator(".conflict-markdown").innerText(), /My draft survives/);
  if (process.env.SCREENSHOT_DIR)
    await page.screenshot({
      path: `${process.env.SCREENSHOT_DIR}/conflict-preview.png`,
      fullPage: true,
    });
  await page.getByRole("button", { name: "Compare text", exact: true }).click();
  await page
    .getByRole("textbox", { name: "Your Markdown", exact: true })
    .fill("Merged **draft** and server changes");
  await page.reload();
  await page.getByRole("heading", { name: "Resolve editing conflict" }).waitFor();
  assert.equal(
    await page.getByRole("textbox", { name: "Your Markdown", exact: true }).inputValue(),
    "Merged **draft** and server changes",
  );
  await page.getByRole("button", { name: "Continue with my draft", exact: true }).click();
  await page.locator(".conflict").waitFor({ state: "hidden" });
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await page.locator(".detail").waitFor();
  assert.equal(
    (await api("task_get", { id: task.id })).body,
    "Merged **draft** and server changes",
  );
  assert.equal((await api("task_get", { id: task.id })).assigned, null);
  // Normal save and cancel restore the original assignee, not the browser marker.
  await api("task_claim", { task: task.id, expected: null, assigned: original });
  await page.goto(`${base}?view=edit#${task.id}`);
  await page.waitForFunction(
    () =>
      document.querySelector(".editor button[type=submit]") &&
      !document.querySelector(".editor button[type=submit]").disabled,
  );
  await page.getByRole("textbox", { name: "Description", exact: true }).fill("Saved safely");
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await page.locator(".detail").waitFor();
  assert.equal((await api("task_get", { id: task.id })).assigned, original);
  await page.goto(`${base}?view=edit#${task.id}`);
  await page.waitForFunction(
    () =>
      document.querySelector(".editor button[type=submit]") &&
      !document.querySelector(".editor button[type=submit]").disabled,
  );
  await page.getByRole("link", { name: "Cancel", exact: true }).click();
  await page.locator(".detail").waitFor();
  assert.equal((await api("task_get", { id: task.id })).assigned, original);
  const abandoned = await context.newPage();
  await abandoned.goto(`${base}?view=edit#${task.id}`);
  await abandoned.waitForFunction(
    () =>
      document.querySelector(".editor button[type=submit]") &&
      !document.querySelector(".editor button[type=submit]").disabled,
  );
  await abandoned.close();
  await page.reload();
  await page.locator(".detail").waitFor();
  await page.getByRole("button", { name: "Release task", exact: true }).click();
  for (let attempt = 0; attempt < 50; attempt++) {
    if ((await api("task_get", { id: task.id })).assigned === original) break;
    await page.waitForTimeout(20);
  }
  assert.equal((await api("task_get", { id: task.id })).assigned, original);
  console.log(
    "Edit claim: reload, two tabs, lost CAS, Markdown merge, atomic save, restore and cancel passed",
  );
} finally {
  await browser.close();
}
