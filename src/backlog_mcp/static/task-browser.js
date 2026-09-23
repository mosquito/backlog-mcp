import { el, field, select, link } from "./dom.js";
import { scopeArgs, scopeControls, scopeLabel } from "./scope.js";
import { taskTable } from "./task-table.js";
import { taskPagination } from "./task-pagination.js";

export async function taskBrowser(target, { call, navigate, principal }) {
  const p = target.params;
  const statuses = ["todo", "doing", "blocked", "done", "cancelled", "deleted"];
  const args = {
    ...scopeArgs(p),
    limit: 50,
    order: p.get("order") === "recent" ? "recent" : "backlog",
  };
  if (p.get("status") === "all") args.status = statuses;
  else if (statuses.includes(p.get("status"))) args.status = [p.get("status")];
  if (["urgent", "high", "normal", "low"].includes(p.get("priority")))
    args.priority = p.get("priority");
  if (p.get("tag")) args.tag = p.get("tag");
  const [data, projects] = await Promise.all([
    call("task_list", args),
    call("project_list", { all_computers: true }),
  ]);
  const scope = scopeControls(p, projects.items);
  const status = select(["", "all", ...statuses], p.get("status") || "", {
    "aria-label": "Task status",
  });
  status.options[0].textContent = "Open tasks";
  status.options[1].textContent = "All statuses";
  const priority = select(["", "urgent", "high", "normal", "low"], p.get("priority") || "", {
    "aria-label": "Task priority",
  });
  priority.options[0].textContent = "All priorities";
  const tag = el("input", {
    value: p.get("tag") || "",
    placeholder: "Any tag",
    "aria-label": "Task tag",
  });
  const order = select(["backlog", "recent"], args.order, { "aria-label": "Task order" });
  const count = el("p", { class: "muted" });
  const table = taskTable([], { showProject: true });
  const pager = taskPagination({
    sources: [{ args, page: data }],
    root: table,
    call,
    append: (items) => table.appendTasks(items),
    changed: (shown, total) => {
      count.textContent = `${shown} of ${total} tasks shown`;
    },
  });
  table.append(pager.sentinel);
  const page = el(
    "section",
    { class: "board-page task-browser" },
    el(
      "div",
      {},
      el(
        "div",
        { class: "row spread" },
        el("h1", {}, "All tasks"),
        link("Projects", "/ui/", "button"),
      ),
      el(
        "form",
        {
          class: "task-browser-filters",
          onsubmit: (event) => {
            event.preventDefault();
            const params = scope.apply(new URLSearchParams({ view: "tasks" }));
            for (const [key, value] of Object.entries({
              status: status.value,
              priority: priority.value,
              tag: tag.value.trim(),
              order: order.value,
            }))
              if (value) params.set(key, value);
            navigate(`/ui/?${params}`);
          },
        },
        scope.element,
        field("Status", status),
        field("Priority", priority),
        field("Tag", tag),
        field("Order", order),
        el("button", { type: "submit" }, "Apply filters"),
      ),
      el("p", { class: "muted" }, scopeLabel(p, principal)),
      count,
    ),
    el(
      "section",
      { class: "board-results" },
      table,
      el("p", { class: "muted" }, "Tasks load as you scroll. Filters apply to every page."),
    ),
  );
  page.mount = () => pager.mount();
  page.dispose = () => pager.dispose();
  return page;
}
