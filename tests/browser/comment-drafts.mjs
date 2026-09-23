// Run against a local UI server with PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs.
import assert from "node:assert/strict";
const { chromium, webkit } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await (process.env.BROWSER === "webkit" ? webkit : chromium).launch(
  process.env.BROWSER === "webkit" ? {} : { channel: "chrome" },
);
const page = await browser.newPage();
const base = process.env.UI_URL || "http://127.0.0.1:3027/ui/";
const comments = new Map([
  [901, []],
  [902, []],
]);
let fail = true;
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
try {
  await page.route("**/mcp", async (route) => {
    const body = route.request().postDataJSON();
    if (body.method !== "tools/call") return route.continue();
    const { name, arguments: args } = body.params;
    let result;
    if (name === "task_get")
      result = {
        id: args.id,
        project: "test/drafts",
        title: `Task ${args.id}`,
        body: "Task description",
        status: "todo",
        priority: "normal",
        assigned: null,
        tags: [],
        links: [],
        comments: comments.get(args.id),
        created_at: 1,
        updated_at: 1,
        created_by: "test",
        updated_by: "test",
      };
    else if (name === "project_board") result = { project: { role: "editor" } };
    else if (name === "task_comment_add") {
      if (fail) {
        fail = false;
        return route.fulfill({ status: 503, body: "Try again" });
      }
      result = { id: 4000, kind: "comment", body: args.body, principal: "test", created_at: 1 };
      comments.get(args.task_id).push(result);
    } else return route.continue();
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: body.id,
        result: { content: [], structuredContent: result },
      }),
    });
  });
  const open = async (id) => {
    await page.goto(`${base}#${id}`);
    await page.locator(".comment-composer").waitFor();
  };
  const expand = () => page.locator(".comment-composer > summary").click();
  const input = page.getByRole("textbox", { name: "Comment", exact: true });
  await open(901);
  assert.equal(await page.locator(".comment-composer").getAttribute("open"), null);
  assert.equal(await page.locator(".relations .empty-state").count(), 0);
  assert.ok((await page.locator(".relations").boundingBox()).height < 150);
  await expand();
  await input.fill("First **draft**");
  await page.getByRole("button", { name: "Preview", exact: true }).click();
  assert.equal(await page.locator(".comment-preview strong").innerText(), "draft");
  await expand();
  await open(902);
  await expand();
  assert.equal(await input.inputValue(), "");
  await input.fill("Second draft");
  await page.reload();
  await page.getByText("Continue comment draft", { exact: true }).waitFor();
  await expand();
  assert.equal(await input.inputValue(), "Second draft");
  await open(901);
  await expand();
  assert.equal(await input.inputValue(), "First **draft**");
  await page.getByRole("button", { name: "Add comment", exact: true }).click();
  await page.locator(".comment-form .error:not([hidden])").waitFor();
  assert.equal(await input.inputValue(), "First **draft**");
  await page.reload();
  await page.getByText("Continue comment draft", { exact: true }).waitFor();
  await expand();
  assert.equal(await input.inputValue(), "First **draft**");
  await page.getByRole("button", { name: "Add comment", exact: true }).click();
  await page.locator("#comment-4000").waitFor();
  assert.equal(await page.locator(".comment-composer").getAttribute("open"), null);
  assert.equal(await page.locator(".comment-composer summary").innerText(), "Add a comment");
  await expand();
  assert.equal(await input.inputValue(), "");
  await open(902);
  await expand();
  assert.equal(await input.inputValue(), "Second draft");
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.deepEqual(errors, []);
  console.log(
    "Per-task drafts, reload, collapse, preview, failed and successful submission: passed",
  );
} finally {
  await browser.close();
}
