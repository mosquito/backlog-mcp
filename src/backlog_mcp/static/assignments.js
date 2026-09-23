import { el, button, field, link, taskRoute, projectRoute } from "./dom.js";

let marker;
function browserMarker() {
  if (marker) return marker;
  const key = "backlog:assignment-marker";
  try {
    marker = sessionStorage.getItem(key);
  } catch {}
  if (!marker) {
    marker = `browser-${crypto.randomUUID()}`;
    try {
      sessionStorage.setItem(key, marker);
    } catch {}
  }
  return marker;
}
export async function assignmentControl(task, { call, principal, notify, refresh }) {
  const { project } = await call("project_board", { project: task.project });
  if (project.role === "reader") return null;
  if (task.assigned && !task.assigned.startsWith(principal + ":")) return null;
  const control = button(task.assigned ? "Release task" : "Claim task", async () => {
    control.disabled = true;
    try {
      await call("task_claim", {
        task: task.id,
        expected: task.assigned || null,
        assigned: task.assigned ? null : `${principal}:${browserMarker()}`,
        ...(task.assigned?.startsWith(`${principal}:ui-edit-`) ? { restore: true } : {}),
      });
      await refresh();
    } catch (error) {
      notify(error);
    } finally {
      control.disabled = false;
    }
  });
  return control;
}

export async function assignmentsPage(target, { call, navigate, principal }) {
  const prefixValue = target.params.get("prefix") || "";
  const offset = Math.max(0, Number(target.params.get("offset")) || 0);
  const limit = 50;
  const data = await call("task_assigned", { prefix: prefixValue || null, offset, limit });
  const prefix = el("input", { value: prefixValue, placeholder: `${principal}:` });
  const href = (nextOffset = 0) => {
    const params = new URLSearchParams({ view: "assigned" });
    if (prefix.value.trim()) params.set("prefix", prefix.value.trim());
    if (nextOffset) params.set("offset", nextOffset);
    return `/ui/?${params}`;
  };
  return el(
    "section",
    { class: "assignments-page" },
    el("h1", {}, "Assigned tasks"),
    el(
      "p",
      { class: "muted" },
      `Across accessible projects and statuses. Default: ${principal}. Closing a task does not release its assignment.`,
    ),
    el(
      "form",
      {
        class: "row",
        onsubmit: (event) => {
          event.preventDefault();
          navigate(href());
        },
      },
      field("Assignment prefix", prefix),
      el("button", { type: "submit" }, "Filter"),
    ),
    el(
      "p",
      { class: "muted" },
      `${data.count} assignments · ${data.items.length ? offset + 1 : 0}–${offset + data.items.length} shown`,
    ),
    ...data.items.map((task) =>
      el(
        "article",
        { class: "panel" },
        link(`#${task.id} ${task.title}`, taskRoute(task.id)),
        el("p", {}, link(task.project, projectRoute(task.project)), ` · ${task.status}`),
        el("p", { class: "assignment-label" }, task.assigned),
      ),
    ),
    data.items.length ? null : el("p", {}, "No matching assignments."),
    el(
      "div",
      { class: "row" },
      offset ? link("Previous", href(Math.max(0, offset - limit)), "button") : null,
      offset + data.items.length < data.count ? link("Next", href(offset + limit), "button") : null,
    ),
  );
}
