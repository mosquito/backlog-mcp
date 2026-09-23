// Run against a local UI with PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs.
import assert from "node:assert/strict";
const { chromium, webkit } = await import(
  process.env.PLAYWRIGHT_MODULE || "playwright"
);
const isWebKit = process.env.BROWSER === "webkit";
const browser = await (isWebKit ? webkit : chromium).launch(
  isWebKit ? {} : { channel: "chrome" },
);
const page = await browser.newPage();
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
let role = "owner";
let items = [
  { principal: "alice@unknown", role: "owner" },
  { principal: "bob@unknown", role: "reader" },
];
const roots = [
  "alice@laptop:/Users/alice/dev/project",
  "bob@workstation:/home/bob/work/project",
  "bob@phone:",
];
try {
  await page.route("**/mcp", async (route) => {
    const body = route.request().postDataJSON();
    if (body.method !== "tools/call") return route.continue();
    const { name, arguments: args } = body.params;
    let result;
    if (name === "whoami") result = { principal: "alice@browser" };
    else if (name === "project_board")
      result = {
        project: {
          name: "Project",
          description: "Shared project description",
          role,
        },
        tasks: { open: 0, done: 0, total: 0 },
      };
    else if (name === "project_members") result = { items, roots };
    else if (name === "project_share") {
      assert.equal(role, "owner");
      if (args.principal === "missing")
        return route.fulfill({
          contentType: "application/json",
          body: JSON.stringify({
            jsonrpc: "2.0",
            id: body.id,
            result: {
              isError: true,
              content: [{ type: "text", text: "Unknown principal" }],
            },
          }),
        });
      const found = items.find((item) => item.principal === args.principal);
      if (found) found.role = args.role;
      else items.push({ principal: args.principal, role: args.role });
      result = { items, roots };
    } else if (name === "task_list") result = { items: [], count: 0 };
    else return route.continue();
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: body.id,
        result: { content: [], structuredContent: result },
      }),
    });
  });
  const url =
    (process.env.UI_URL || "http://127.0.0.1:3027/ui/") +
    "?project=alice%2Fproject&view=table";
  await page.goto(url);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".project-settings").waitFor();
  assert.equal(new URL(page.url()).searchParams.get("view"), "settings");
  assert.match(await page.locator("#breadcrumbs").innerText(), /Settings/);
  for (const width of [1280, 390])
    for (const colorScheme of ["light", "dark"]) {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme });
      assert.equal(
        await page.evaluate(
          () => document.documentElement.scrollWidth > innerWidth,
        ),
        false,
      );
      if (process.env.SCREENSHOT_DIR)
        await page.screenshot({
          path: `${process.env.SCREENSHOT_DIR}/settings-${width}-${colorScheme}.png`,
          fullPage: true,
        });
    }
  await page
    .getByRole("button", { name: "Share project", exact: true })
    .click();
  await page.getByLabel("Principal", { exact: true }).fill("missing");
  await page.getByRole("button", { name: "Grant access" }).click();
  await page
    .getByRole("alert")
    .filter({ hasText: "Unknown principal" })
    .waitFor();
  await page.getByLabel("Principal", { exact: true }).fill("charlie");
  await page.getByRole("button", { name: "Grant access" }).click();
  await page.locator(".action-dialog").waitFor({ state: "detached" });
  assert.match(await page.locator(".project-members").innerText(), /charlie/);
  await page
    .locator(".member-row")
    .filter({ hasText: "bob@unknown" })
    .getByRole("button")
    .click();
  await page.getByRole("combobox", { name: /Role/ }).selectOption("editor");
  await page.getByRole("button", { name: "Save role" }).click();
  await page.locator(".action-dialog").waitFor({ state: "detached" });
  assert.match(
    await page
      .locator(".member-row")
      .filter({ hasText: "bob@unknown" })
      .innerText(),
    /editor/,
  );
  for (role of ["reader", "editor"]) {
    await page.reload();
    await page.locator(".project-settings").waitFor();
    assert.equal(
      await page
        .getByRole("button", { name: "Share project", exact: true })
        .count(),
      0,
    );
    assert.equal(
      await page
        .getByRole("button", { name: "Change role", exact: true })
        .count(),
      0,
    );
    assert.equal(await page.locator(".binding-row").count(), 3);
  }
  await page.getByRole("button", { name: "Table", exact: true }).click();
  await page.locator(".board-page").waitFor();
  assert.equal(new URL(page.url()).searchParams.get("view"), "table");
  assert.deepEqual(errors, []);
  console.log(
    "Settings: navigation, sharing, role change, errors, read-only roles and responsive layouts passed",
  );
} finally {
  await browser.close();
}
