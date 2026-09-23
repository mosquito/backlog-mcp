import { el, link } from "./dom.js";
import { scopeArgs, scopeControls, scopeLabel } from "./scope.js";

export async function statisticsPage(target, { call, navigate, principal }) {
  const [stats, projects] = await Promise.all([
    call("stats", scopeArgs(target.params)),
    call("project_list", { all_computers: true }),
  ]);
  const scope = scopeControls(target.params, projects.items);
  const breakdown = (title, values) =>
    el(
      "section",
      { class: "panel" },
      el("h2", {}, title),
      Object.keys(values).length
        ? el(
            "dl",
            { class: "stats-breakdown" },
            ...Object.entries(values).flatMap(([key, count]) => [
              el("dt", {}, key),
              el("dd", {}, String(count)),
            ]),
          )
        : el("p", { class: "muted" }, "No data."),
    );
  return el(
    "section",
    { class: "statistics-page" },
    el(
      "div",
      { class: "row spread" },
      el("h1", {}, "Statistics"),
      link("Projects", "/ui/", "button"),
    ),
    el(
      "form",
      {
        class: "row",
        onsubmit: (event) => {
          event.preventDefault();
          const p = scope.apply(new URLSearchParams({ view: "stats" }));
          navigate(`/ui/?${p}`);
        },
      },
      scope.element,
      el("button", { type: "submit" }, "Apply scope"),
    ),
    el("p", { class: "muted scope-description" }, scopeLabel(target.params, principal)),
    el(
      "div",
      { class: "stats-totals" },
      ...[
        ["Total tasks", stats.total],
        ["Open", stats.open],
        ["Closed", stats.closed],
        ["Notes", stats.memory],
      ].map(([title, count]) =>
        el("div", { class: "panel" }, el("span", {}, title), el("strong", {}, String(count))),
      ),
    ),
    el(
      "p",
      { class: "muted" },
      "Counts include all matches, not just loaded pages. Closed means done or cancelled; deleted tasks are counted separately by status. Notes include shared notes.",
    ),
    el(
      "div",
      { class: "stats-groups" },
      breakdown("By status", stats.by_status),
      breakdown("By priority", stats.by_priority),
    ),
    el(
      "section",
      { class: "panel" },
      el("h2", {}, "Tags"),
      ...(stats.tags.length
        ? stats.tags.map((tag) => {
            const params = new URLSearchParams(target.params);
            params.set("view", "tasks");
            params.set("status", "all");
            params.set("tag", tag);
            return link(tag, `/ui/?${params}`, "tag");
          })
        : [el("p", { class: "muted" }, "No tags.")]),
    ),
  );
}
