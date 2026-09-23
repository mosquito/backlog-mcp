import { el, button, link, select, field, projectRoute, taskRoute, modal } from "./dom.js";

const fields = ["title", "priority", "status", "tags", "body"];
const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);
export function changed(base, values) {
  return Object.fromEntries(
    fields.filter((key) => !equal(base[key], values[key])).map((key) => [key, values[key]]),
  );
}
export function editor(task, project, { call, notify, navigate, markdown, principal }) {
  const key = `backlog:draft:${principal}:${task ? task.id : "new:" + project}`;
  let base = task || { title: "", priority: "normal", status: "todo", tags: [], body: "" };
  let saved;
  try {
    saved = JSON.parse(localStorage.getItem(key));
  } catch {
    saved = null;
  }
  let values = saved?.values || base;
  if (task && saved?.base) base = saved.base;
  const title = el("input", { name: "title", required: true, value: values.title });
  const priority = select(["urgent", "high", "normal", "low"], values.priority, {
    name: "priority",
  });
  const status = select(["todo", "doing", "blocked", "done", "cancelled"], values.status, {
    name: "status",
  });
  const tags = el("input", {
    name: "tags",
    value: (values.tags || []).join(", "),
    placeholder: "Comma-separated tags",
  });
  const body = el("textarea", {
    name: "body",
    id: "task-description",
    value: values.body || "",
    "aria-label": "Description",
  });
  const preview = el("div", { class: "editor-preview markdown", hidden: true });
  const conflict = el("section", { class: "panel conflict", hidden: true });
  const submit = el(
    "button",
    { type: "submit", class: "primary" },
    task ? "Save changes" : "Create task",
  );
  function read() {
    return {
      title: title.value,
      priority: priority.value,
      status: status.value,
      tags: [
        ...new Set(
          tags.value
            .split(",")
            .map((t) => t.trim())
            .filter(Boolean),
        ),
      ],
      body: body.value,
    };
  }
  function persist() {
    try {
      localStorage.setItem(key, JSON.stringify({ base, values: read() }));
    } catch {
      notify(new Error("The browser could not save this draft."));
    }
  }
  function fill(next) {
    title.value = next.title;
    priority.value = next.priority;
    status.value = next.status;
    tags.value = next.tags.join(", ");
    body.value = next.body;
  }
  const form = el(
    "form",
    { class: "editor" },
    link("← " + project, projectRoute(project)),
    el("div", { class: "eyebrow", style: "margin-top:24px" }, task ? `Task #${task.id}` : "Create"),
    el("h1", {}, task ? "Edit task" : "New task"),
    el(
      "p",
      { class: "muted" },
      saved
        ? "Your saved draft has been restored. Changes are saved automatically in this browser."
        : "Your draft is saved automatically in this browser.",
    ),
    el(
      "div",
      { class: "panel" },
      field("Title", title),
      el(
        "div",
        { class: "form-grid" },
        field("Priority", priority),
        field("Status", status),
        field("Tags", tags),
      ),
      el("label", { htmlFor: "task-description" }, "Description"),
      el(
        "div",
        { class: "editor-tabs", "aria-label": "Description view" },
        button("Text", () => {
          body.hidden = false;
          preview.hidden = true;
          updateTabs();
        }),
        button("Preview", () => {
          preview.replaceChildren(markdown(body.value));
          preview.hidden = false;
          body.hidden = true;
          updateTabs();
        }),
      ),
      body,
      preview,
    ),
    conflict,
    el(
      "div",
      { class: "form-actions" },
      submit,
      link("Cancel", task ? taskRoute(task.id) : projectRoute(project), "button"),
      button(
        "Discard draft",
        async () => {
          if (
            await modal({
              title: "Discard draft?",
              description: "Your unsaved changes will be removed from this browser.",
              action: "Discard draft",
              danger: true,
            })
          ) {
            localStorage.removeItem(key);
            navigate(task ? taskRoute(task.id) : projectRoute(project));
          }
        },
        "quiet",
      ),
    ),
  );
  function updateTabs() {
    const tabs = form.querySelectorAll(".editor-tabs button");
    tabs[0].setAttribute("aria-pressed", String(!body.hidden));
    tabs[1].setAttribute("aria-pressed", String(body.hidden));
  }
  updateTabs();
  form.addEventListener("input", persist);
  form.addEventListener("change", persist);
  async function save() {
    values = read();
    persist();
    submit.disabled = true;
    try {
      if (!task) {
        const created = await call("task_add", { project, ...values });
        localStorage.removeItem(key);
        navigate(taskRoute(created.id));
        return;
      }
      const current = await call("task_get", { id: task.id });
      // This detects known changes, but the API has no atomic compare-and-swap.
      if (
        current.updated_at !== base.updated_at ||
        fields.some((key) => !equal(current[key], base[key])) ||
        current.project !== base.project
      ) {
        showConflict(current, values);
        return;
      }
      const updates = changed(base, values);
      if (Object.keys(updates).length) await call("task_update", { id: task.id, ...updates });
      localStorage.removeItem(key);
      navigate(taskRoute(task.id));
    } catch (error) {
      notify(error);
    } finally {
      submit.disabled = false;
    }
  }
  function showConflict(current, ours) {
    conflict.hidden = false;
    const changedKeys = fields.filter(
      (key) => !equal(base[key], current[key]) || !equal(base[key], ours[key]),
    );
    conflict.replaceChildren(
      el("h2", {}, "This task changed while you were editing"),
      el("p", {}, "Review the saved version and your draft. Saving checks the server again."),
      ...changedKeys.map((key) =>
        el(
          "details",
          { open: true },
          el("summary", {}, key),
          el(
            "div",
            { class: "conflict-values" },
            ...[
              ["Base", base[key]],
              ["Current", current[key]],
              ["Your draft", ours[key]],
            ].map(([label, value]) =>
              el(
                "div",
                {},
                el("strong", {}, label),
                el("pre", {}, typeof value === "string" ? value : JSON.stringify(value)),
              ),
            ),
          ),
        ),
      ),
      button("Use my changes", () => {
        // Keep concurrent fields that this draft did not edit.
        const updates = changed(base, ours);
        base = current;
        fill({ ...current, ...updates });
        conflict.hidden = true;
        persist();
      }),
      button("Merge manually", () => {
        const merged = { ...current, ...changed(base, ours) };
        base = current;
        fill(merged);
        persist();
        conflict.append(
          el("p", {}, "The current version is now the base. Edit the fields below, then save."),
        );
        body.hidden = false;
        preview.hidden = true;
        updateTabs();
        body.focus();
      }),
    );
    conflict.scrollIntoView({ block: "start", behavior: "smooth" });
  }
  form.onsubmit = (event) => {
    event.preventDefault();
    save();
  };
  return form;
}
