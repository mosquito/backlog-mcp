import { el, link, date, taskRoute } from "./dom.js";

export function taskTable(tasks, { showProject = false } = {}) {
  const table = el(
    "table",
    { class: "task-table" },
    el("caption", { class: "sr-only" }, "Project tasks"),
    el(
      "thead",
      {},
      el(
        "tr",
        {},
        ...["Task", "Status", "Priority", "Assigned", "Updated", "Tags"].map((title) =>
          el("th", { scope: "col" }, title),
        ),
      ),
    ),
    el("tbody", {}, ...tasks.map((task) => taskRow(task, showProject))),
  );
  const region = el(
    "div",
    {
      class: "task-table-region",
      role: "region",
      "aria-label": "Task table. Scroll to see all columns.",
      tabIndex: 0,
    },
    table,
  );
  region.appendTasks = (items) => {
    table.querySelector("tbody").append(...items.map((task) => taskRow(task, showProject)));
  };
  return region;
}

function taskRow(task, showProject) {
  return el(
    "tr",
    { "data-task": task.id },
    el(
      "th",
      { scope: "row", class: "task-table-title" },
      el("span", { class: "task-table-id" }, `#${task.id}`),
      link(task.title, taskRoute(task.id)),
      showProject ? el("small", { class: "muted" }, task.project) : null,
    ),
    el("td", {}, el("span", { class: `task-status status-${task.status}` }, task.status)),
    el("td", {}, el("span", { class: `priority ${task.priority}` }, task.priority)),
    el(
      "td",
      { class: "task-table-assigned" },
      el("span", { title: task.assigned || "Unassigned" }, task.assigned || "Unassigned"),
    ),
    el(
      "td",
      { class: "task-table-updated" },
      task.updated_at
        ? el(
            "time",
            { dateTime: new Date(task.updated_at * 1000).toISOString() },
            date(task.updated_at),
          )
        : "—",
    ),
    el(
      "td",
      { class: "task-table-tags" },
      ...(task.tags?.length
        ? task.tags.map((tag) => el("span", { class: "tag" }, tag))
        : [el("span", { class: "muted" }, "—")]),
    ),
  );
}
