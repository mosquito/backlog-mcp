import { el, button, link, field, select, memoryRoute } from "./dom.js";

export function memoryEditor(
  project,
  projects,
  { call, navigate, markdown, principal },
  existing = null,
) {
  const key = `backlog:memory-draft:${principal}:${existing ? "note:" + existing.id : project || "all"}`;
  let saved;
  try {
    saved = JSON.parse(localStorage.getItem(key));
  } catch {
    saved = null;
  }
  const writable = projects.filter((p) => ["owner", "editor"].includes(p.role));
  const names = writable.map((p) => p.full_name);
  const initial = saved || (existing ? { ...existing, tags: existing.tags.join(", ") } : {});
  const base = saved?.base || existing;
  const scope = select(["", ...names], saved?.project || project, { required: true });
  scope.options[0].text = "Choose a project";
  scope.disabled = Boolean(existing);
  const title = el("input", { required: true, value: initial.title || "" });
  const body = el("textarea", { value: initial.body || "", rows: 8 });
  const tags = el("input", { value: initial.tags || "", placeholder: "Comma-separated tags" });
  const shared = el("input", { type: "checkbox", checked: initial.shared === true });
  const count = el("small", { class: "muted", "aria-live": "polite" });
  const error = el("p", { class: "error", role: "alert", hidden: true });
  const preview = el("div", { class: "markdown editor-preview", hidden: true });
  const submit = el(
    "button",
    { type: "submit", class: "primary", disabled: !names.length },
    existing ? "Save changes" : "Add note",
  );
  const back = memoryRoute(project, new URLSearchParams(existing ? { note: existing.id } : {}));
  const form = el(
    "form",
    { class: "editor" },
    link("← Memory", back),
    el("h1", {}, existing ? "Edit note" : "Add note"),
    el(
      "p",
      { class: "muted" },
      saved
        ? "Your saved draft has been restored."
        : "Your draft is saved automatically in this browser.",
    ),
    !names.length
      ? el("p", { class: "error" }, "You need editor access to a project to add a note.")
      : null,
    el(
      "div",
      { class: "panel" },
      field("Project", scope),
      field("Title", title),
      field("Tags", tags),
      field("Content", body),
      count,
      el(
        "div",
        { class: "editor-tabs" },
        button("Preview", () => {
          preview.hidden = !preview.hidden;
          preview.replaceChildren(markdown(body.value));
        }),
      ),
      preview,
      el("label", { class: "inline" }, shared, "Shared across accessible projects"),
    ),
    error,
    el(
      "div",
      { class: "form-actions" },
      submit,
      link("Cancel", back, "button"),
      button(
        "Discard draft",
        () => {
          localStorage.removeItem(key);
          navigate(back);
        },
        "quiet",
      ),
    ),
  );
  function values() {
    return {
      project: scope.value,
      title: title.value,
      body: body.value,
      tags: tags.value,
      shared: shared.checked,
    };
  }
  function update() {
    const length = [...body.value].length;
    count.textContent = `${length} / 1024 characters`;
    body.setCustomValidity(length > 1024 ? "Use at most 1024 characters." : "");
    title.setCustomValidity(title.value.trim() ? "" : "Enter a title.");
  }
  function persist() {
    update();
    try {
      localStorage.setItem(key, JSON.stringify({ ...values(), base }));
    } catch {
      error.hidden = false;
      error.textContent = "The browser could not save this draft.";
    }
  }
  form.addEventListener("input", persist);
  form.addEventListener("change", persist);
  update();
  form.onsubmit = async (event) => {
    event.preventDefault();
    persist();
    if (!form.reportValidity()) return;
    submit.disabled = true;
    error.hidden = true;
    try {
      const data = values();
      if (existing) {
        const current = await call("memory_get", { id: existing.id });
        if (
          ["title", "body", "tags", "shared", "updated_at"].some(
            (k) => JSON.stringify(current[k]) !== JSON.stringify(base[k]),
          )
        )
          throw new Error(
            "This note changed while you were editing. Your draft is saved. Copy your changes, then discard the draft and reopen the note to review the latest version.",
          );
      }
      const note = await call("memory_save", {
        ...data,
        title: data.title.trim(),
        tags: [
          ...new Set(
            data.tags
              .split(",")
              .map((t) => t.trim())
              .filter(Boolean),
          ),
        ],
        ...(existing ? { id: existing.id } : { create_only: true }),
      });
      try {
        localStorage.removeItem(key);
      } catch {
        /* The note was saved successfully. */
      }
      navigate(memoryRoute(note.project, new URLSearchParams({ note: note.id })));
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    } finally {
      submit.disabled = false;
    }
  };
  return form;
}
