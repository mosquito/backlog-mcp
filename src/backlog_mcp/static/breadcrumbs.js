import { el, link, projectRoute, memoryRoute, taskRoute } from "./dom.js";

export function breadcrumbs(target, project) {
  const items = [{ label: "Projects", href: "/ui/" }];
  if (project) items.push({ label: project, href: projectRoute(project) });
  switch (target.kind) {
    case "assigned":
      items.push({ label: "Assigned tasks" });
      break;
    case "settings":
      items.push({ label: "Settings" });
      break;
    case "board":
      if (target.params.get("status") === "deleted") items.push({ label: "Deleted tasks" });
      break;
    case "task":
    case "edit":
      items.push({ label: `Task #${target.id}`, href: taskRoute(target.id) });
      if (target.kind === "edit") items.push({ label: "Edit" });
      break;
    case "new":
      items.push({ label: "New task" });
      break;
    case "memory":
      items.push({ label: "Memory", href: memoryRoute(project) });
      if (target.id)
        items.push({
          label: `Note #${target.id}`,
          href: memoryRoute(project, new URLSearchParams({ note: target.id })),
        });
      if (target.params.get("create") === "true") items.push({ label: "New note" });
      else if (target.params.get("edit") === "true") items.push({ label: "Edit" });
      break;
  }
  return el(
    "ol",
    {},
    ...items.map((item, index) =>
      el(
        "li",
        {},
        index === items.length - 1
          ? el("span", { "aria-current": "page" }, item.label)
          : link(item.label, item.href),
      ),
    ),
  );
}
