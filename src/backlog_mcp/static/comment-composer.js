import { el, button } from "./dom.js";

export function commentComposer({ taskId, draftKey, call, markdown, onAdded }) {
  const body = el("textarea", {
    placeholder: "Write a comment in Markdown…",
    "aria-label": "Comment",
  });
  const draftStatus = el("small", { class: "muted", role: "status" });
  const summary = el("summary", {}, "Add a comment");
  function showDraft() {
    summary.textContent = body.value ? "Continue comment draft" : "Add a comment";
  }
  try {
    body.value = localStorage.getItem(draftKey) || "";
    if (body.value) draftStatus.textContent = "Draft saved on this device";
  } catch {
    draftStatus.textContent = "Draft storage is unavailable in this browser.";
  }
  showDraft();
  body.oninput = () => {
    showDraft();
    try {
      if (body.value) localStorage.setItem(draftKey, body.value);
      else localStorage.removeItem(draftKey);
      draftStatus.textContent = body.value ? "Draft saved on this device" : "";
    } catch {
      draftStatus.textContent = "Could not save this draft. Keep this page open.";
    }
  };
  const preview = el("div", { class: "comment-preview markdown", hidden: true });
  const error = el("p", { class: "error", role: "alert", hidden: true });
  const write = button("Write", () => changeView(false));
  const previewButton = button("Preview", () => changeView(true));
  function changeView(showPreview) {
    if (showPreview) preview.replaceChildren(markdown(body.value));
    body.hidden = showPreview;
    preview.hidden = !showPreview;
    write.setAttribute("aria-pressed", String(!showPreview));
    previewButton.setAttribute("aria-pressed", String(showPreview));
  }
  const submit = el("button", { type: "submit", class: "primary" }, "Add comment");
  const form = el(
    "form",
    { class: "comment-form" },
    el("div", { class: "editor-tabs" }, write, previewButton),
    body,
    preview,
    error,
    el(
      "div",
      { class: "form-actions" },
      el("div", {}, el("small", { class: "muted" }, "Markdown supported"), draftStatus),
      submit,
    ),
  );
  const composer = el("details", { class: "comment-composer" }, summary, form);
  changeView(false);
  form.onsubmit = async (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    if (!body.value.trim()) {
      changeView(false);
      error.textContent = "Write a comment before sending.";
      error.hidden = false;
      body.focus();
      return;
    }
    submit.disabled = true;
    body.disabled = true;
    error.hidden = true;
    let added;
    try {
      added = await call("task_comment_add", { task_id: taskId, body: body.value });
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
      return;
    } finally {
      submit.disabled = false;
      body.disabled = false;
    }
    body.value = "";
    preview.replaceChildren();
    showDraft();
    try {
      localStorage.removeItem(draftKey);
      draftStatus.textContent = "";
    } catch {
      draftStatus.textContent = "Comment sent, but the saved draft could not be cleared.";
    }
    composer.open = false;
    await onAdded(added);
  };
  return composer;
}
