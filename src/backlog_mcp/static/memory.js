import { el, button, link, field, select, date, projectRoute, memoryRoute } from "./dom.js";

export async function memoryPage(target, { call, navigate, markdown }) {
  const params = target.params;
  const requestedOffset = Number(params.get("offset"));
  const offset = Number.isSafeInteger(requestedOffset) && requestedOffset > 0 ? requestedOffset : 0;
  const limit = 25;
  const args = { limit, offset };
  if (target.project) args.project = target.project;
  else args.all_computers = true;
  if (params.get("shared") === "true") args.shared = true;
  if (params.get("shared") === "false") args.shared = false;
  if (params.get("tag")) args.tag = params.get("tag");
  const [notes, projects] = await Promise.all([
    call("memory_list", args),
    call("project_list", { all_computers: true }),
  ]);
  const scope = select(
    [
      "",
      ...new Set([
        ...projects.items.map((p) => p.full_name),
        ...(target.project ? [target.project] : []),
      ]),
    ],
    target.project || "",
    { "aria-label": "Memory project" },
  );
  scope.options[0].text = "All projects";
  const visibility = select(["", "true", "false"], params.get("shared") || "", {
    "aria-label": "Memory visibility",
  });
  ["All notes", "Shared only", "Project-only notes"].forEach(
    (label, i) => (visibility.options[i].text = label),
  );
  const tag = el("input", {
    value: params.get("tag") || "",
    placeholder: "Any tag",
    "aria-label": "Memory tag",
  });
  const change = (updates) => {
    const p = new URLSearchParams(params);
    for (const [key, value] of Object.entries(updates)) {
      if (value === null || value === "") p.delete(key);
      else p.set(key, String(value));
    }
    return memoryRoute(p.get("project") || "", p);
  };
  const filters = el(
    "form",
    { class: "memory-filters" },
    field("Project", scope),
    field("Visibility", visibility),
    field("Tag", tag),
    el("button", { type: "submit" }, "Apply filters"),
  );
  filters.onsubmit = (event) => {
    event.preventDefault();
    navigate(
      change({
        project: scope.value,
        shared: visibility.value,
        tag: tag.value.trim(),
        offset: null,
        note: null,
      }),
    );
  };
  const list = el(
    "nav",
    { class: "memory-list", "aria-label": "Memory notes" },
    ...notes.items.map((note) => {
      const item = link("", change({ note: note.id }), "memory-item");
      item.onclick = (event) => {
        if (
          event.button === 0 &&
          !event.metaKey &&
          !event.ctrlKey &&
          !event.shiftKey &&
          !event.altKey
        )
          window.scrollTo(0, 0);
      };
      if (note.id === target.id) item.setAttribute("aria-current", "page");
      item.append(
        el(
          "div",
          { class: "memory-item-meta" },
          `Note ${note.id}`,
          ...(note.shared ? [el("span", { class: "tag" }, "Shared")] : []),
        ),
        el("strong", {}, note.title),
        el("p", {}, note.snippet || "No preview available."),
        el("small", {}, note.project),
      );
      return item;
    }),
  );
  if (!notes.items.length)
    list.append(
      el(
        "div",
        { class: "memory-empty" },
        el("strong", {}, "No notes found"),
        el("p", { class: "muted" }, "Choose another project or change the filters."),
      ),
    );
  const previous = button("Previous", () =>
    navigate(change({ offset: Math.max(0, offset - limit), note: null })),
  );
  previous.disabled = offset === 0;
  const next = button("Next", () => navigate(change({ offset: offset + limit, note: null })));
  next.disabled = offset + notes.items.length >= notes.count;
  const pagination = el(
    "div",
    { class: "memory-pagination" },
    previous,
    el(
      "span",
      { class: "muted" },
      notes.items.length
        ? `${offset + 1}–${offset + notes.items.length} of ${notes.count}`
        : `0 of ${notes.count}`,
    ),
    next,
  );
  const detail = el("article", { class: "panel memory-detail" });
  if (target.id) {
    try {
      const note = await call("memory_get", { id: target.id });
      detail.append(
        link("← Back to notes", change({ note: null }), "memory-back"),
        el("div", { class: "eyebrow" }, `Memory · ${note.id}`),
        el("h2", {}, note.title),
        el(
          "div",
          { class: "memory-detail-meta" },
          link(note.project, projectRoute(note.project)),
          el("span", { class: "tag" }, note.shared ? "Shared across projects" : "Project note"),
        ),
        el(
          "div",
          { class: "memory-tags" },
          ...(note.tags || []).map((t) => el("span", { class: "tag" }, t)),
        ),
        el("div", { class: "markdown memory-body" }, markdown(note.body || "No content.")),
        el(
          "dl",
          { class: "memory-audit" },
          el("dt", {}, "Updated"),
          el("dd", {}, `${date(note.updated_at)} · ${note.updated_by || "—"}`),
          el("dt", {}, "Created"),
          el("dd", {}, `${date(note.created_at)} · ${note.created_by || "—"}`),
        ),
      );
    } catch (error) {
      detail.append(
        link("← Back to notes", change({ note: null }), "memory-back"),
        el("h2", {}, "Could not load this note"),
        el("p", { class: "error", role: "alert" }, error.message),
        button("Try again", () => navigate(change({ note: target.id }))),
      );
    }
  } else
    detail.append(
      el(
        "div",
        { class: "memory-placeholder" },
        el("h2", {}, "Select a note"),
        el("p", { class: "muted" }, "Read project context, decisions, and shared agreements."),
      ),
    );
  return el(
    "section",
    { class: `memory-page${target.id ? " has-selection" : ""}` },
    el("div", { class: "eyebrow" }, "Knowledge"),
    el(
      "div",
      { class: "row spread" },
      el("h1", {}, "Memory"),
      link(
        target.project ? "Back to tasks" : "Projects",
        target.project ? projectRoute(target.project) : "/ui/",
        "button",
      ),
    ),
    el(
      "p",
      { class: "subtitle" },
      `${notes.count} notes · ${target.project || "All accessible projects"}`,
    ),
    filters,
    el(
      "div",
      { class: "memory-layout" },
      el("aside", { class: "memory-sidebar" }, list, pagination),
      detail,
    ),
  );
}
