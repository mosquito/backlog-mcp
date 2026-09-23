import { el, button, link, select, field, projectRoute, taskRoute, modal } from "./dom.js";
import { editClaim } from "./editor-claim.js";

const fields = ["title", "priority", "status", "tags", "body"];
const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);
export function changed(base, values) {
  return Object.fromEntries(
    fields.filter((key) => !equal(base[key], values[key])).map((key) => [key, values[key]]),
  );
}
export function editor(task, project, actions) {
  const { call, notify, navigate, markdown, principal } = actions;
  const claim = task ? editClaim(task, actions) : null;
  let ready = Promise.resolve(),
    disposed = false;

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
  const editFields = form.querySelector(".panel");
  function updateTabs() {
    const tabs = form.querySelectorAll(".editor-tabs button");
    tabs[0].setAttribute("aria-pressed", String(!body.hidden));
    tabs[1].setAttribute("aria-pressed", String(body.hidden));
  }
  updateTabs();
  form.addEventListener("input", persist);
  form.addEventListener("change", persist);
  const differs = (current) =>
    fields.some((key) => !equal(current[key], base[key])) || current.project !== base.project;
  form.mount = () => {
    if (!task) return;
    submit.disabled = true;
    ready = (async () => {
      try {
        const current = await claim.acquire(await call("task_get", { id: task.id }));
        if (!disposed && differs(current)) showConflict(current, read());
      } catch (error) {
        if (!disposed) {
          showConflict(await call("task_get", { id: task.id }), read(), error.message);
        }
      } finally {
        submit.disabled = !conflict.hidden;
      }
    })().catch(notify);
  };
  form.dispose = async () => {
    disposed = true;
    await ready;
    try {
      await claim?.release();
    } catch (error) {
      notify(error);
    }
  };
  async function save() {
    values = read();
    persist();
    submit.disabled = true;
    form.inert = true;
    try {
      if (!task) {
        const created = await call("task_add", { project, ...values });
        localStorage.removeItem(key);
        navigate(taskRoute(created.id));
        return;
      }
      await ready;
      const current = await call("task_get", { id: task.id });
      if (!claim.token || current.assigned !== claim.token || differs(current)) {
        showConflict(current, values);
        return;
      }
      const updates = changed(base, values);
      await call("task_update", {
        id: task.id,
        ...updates,
        expected_assigned: claim.token,
        release_edit: true,
      });
      claim.saved();
      localStorage.removeItem(key);
      navigate(taskRoute(task.id));
    } catch (error) {
      if (!task) notify(error);
      if (task) {
        try {
          showConflict(await call("task_get", { id: task.id }), read(), error.message);
        } catch (readError) {
          notify(readError);
        }
      }
    } finally {
      form.inert = false;
      submit.disabled = !conflict.hidden;
    }
  }
  function showConflict(current, ours, reason = "The saved content or edit claim changed.") {
    conflict.hidden = false;
    editFields.hidden = true;
    submit.disabled = true;
    if (/AssignmentConflict|assignment conflict|expected_assigned/.test(reason))
      reason = "The edit claim changed. Your draft was not saved.";
    else if (/being edited|another browser tab/.test(reason))
      reason = "Another editor has this task open. Your draft is kept; retry after they finish.";
    const serverBody = el("textarea", {
      readOnly: true,
      value: current.body || "",
      "aria-label": "Server Markdown",
    });
    const mergedBody = el("textarea", { value: ours.body || "", "aria-label": "Your Markdown" });
    const serverPreview = el(
      "div",
      { class: "markdown", hidden: true },
      markdown(current.body || ""),
    );
    const oursPreview = el("div", { class: "markdown", hidden: true });
    mergedBody.oninput = () => {
      body.value = mergedBody.value;
      persist();
    };
    const accept = async (useServer) => {
      const controls = [...conflict.querySelectorAll("button")];
      controls.forEach((control) => (control.disabled = true));
      try {
        const latest = await call("task_get", { id: task.id });
        if (
          fields.some((key) => !equal(latest[key], current[key])) ||
          latest.project !== current.project
        ) {
          showConflict(latest, read(), "The server changed again. Review the new version.");
          return;
        }
        const claimed = await claim.acquire(latest);
        if (
          fields.some((key) => !equal(claimed[key], current[key])) ||
          claimed.project !== current.project
        ) {
          showConflict(
            claimed,
            read(),
            "The server changed while acquiring the claim. Review it again.",
          );
          return;
        }
        const updates = { ...changed(base, read()), body: read().body };
        base = claimed;
        fill(useServer ? claimed : { ...claimed, ...updates });
        conflict.hidden = true;
        editFields.hidden = false;
        submit.disabled = false;
        persist();
        body.hidden = false;
        preview.hidden = true;
        updateTabs();
        body.focus();
      } catch (error) {
        notify(error);
      } finally {
        controls.forEach((control) => (control.disabled = false));
      }
    };
    conflict.replaceChildren(
      el("h2", {}, "Resolve editing conflict"),
      el("p", { role: "status" }, reason),
      el(
        "p",
        {},
        "Your draft is kept. Compare the server version with your Markdown, edit your draft, then continue and save.",
      ),
      el(
        "p",
        { class: "muted" },
        `Current assignment: ${current.assigned ? current.assigned.split(":")[0] : "Unassigned"}`,
      ),
      el(
        "div",
        { class: "row" },
        button("Compare text", () => {
          serverBody.hidden = mergedBody.hidden = false;
          serverPreview.hidden = oursPreview.hidden = true;
        }),
        button("Compare preview", () => {
          oursPreview.replaceChildren(markdown(mergedBody.value));
          serverBody.hidden = mergedBody.hidden = true;
          serverPreview.hidden = oursPreview.hidden = false;
        }),
      ),
      el(
        "div",
        { class: "conflict-markdown" },
        el("section", {}, el("h3", {}, "Saved on server"), serverBody, serverPreview),
        el("section", {}, el("h3", {}, "Your draft / merged result"), mergedBody, oursPreview),
      ),
      ...fields
        .filter((key) => key !== "body" && !equal(current[key], ours[key]))
        .map((key) =>
          el(
            "p",
            {},
            `${key}: server ${JSON.stringify(current[key])}; your draft ${JSON.stringify(ours[key])}`,
          ),
        ),
      el(
        "div",
        { class: "row" },
        button("Continue with my draft", () => accept(false)),
        button("Use server version", () => accept(true)),
        button("Refresh server version", async () => {
          try {
            showConflict(await call("task_get", { id: task.id }), read());
          } catch (error) {
            notify(error);
          }
        }),
      ),
    );
    conflict.scrollIntoView({ block: "start", behavior: "smooth" });
  }
  form.onsubmit = (event) => {
    event.preventDefault();
    save();
  };
  return form;
}
