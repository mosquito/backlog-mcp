import { el, button, link, field, select, date, projectRoute, memoryRoute } from "./dom.js";
import { memoryEditor } from "./memory-editor.js";

export async function memoryPage(target, actions) {
  const { call, navigate, markdown } = actions;
  const params = target.params;
  if (
    (params.get("create") === "true" && !target.id) ||
    (params.get("edit") === "true" && target.id)
  ) {
    const projects = await call("project_list", { all_computers: true });
    const note = target.id ? await call("memory_get", { id: target.id }) : null;
    return memoryEditor(note?.project || target.project, projects.items, actions, note);
  }
  const offset = 0;
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
  function noteItem(note) {
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
  }
  const list = el(
    "nav",
    { class: "memory-list", "aria-label": "Memory notes" },
    ...notes.items.map(noteItem),
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
  let nextOffset = notes.items.length;
  let total = notes.count;
  let loading = false;
  let disposed = false;
  const seen = new Set(notes.items.map((note) => note.id));
  const sentinel = el("div", { class: "memory-pagination", role: "status", "aria-live": "polite" });
  const observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) loadMore();
    },
    { rootMargin: "200px" },
  );
  function state() {
    sentinel.replaceChildren(
      el(
        "span",
        { class: "muted" },
        nextOffset < total
          ? `${seen.size} of ${total} notes · Scroll for more`
          : `${seen.size} notes · All loaded`,
      ),
    );
  }
  async function loadMore() {
    if (loading || disposed || nextOffset >= total || !list.isConnected) return;
    loading = true;
    list.setAttribute("aria-busy", "true");
    observer.unobserve(sentinel);
    sentinel.replaceChildren(el("span", {}, "Loading more notes…"));
    try {
      const page = await call("memory_list", { ...args, offset: nextOffset });
      if (disposed || !list.isConnected) return;
      nextOffset += page.items.length;
      total = page.count;
      for (const note of page.items) {
        if (seen.has(note.id)) continue;
        seen.add(note.id);
        list.insertBefore(noteItem(note), sentinel);
      }
      if (!page.items.length) total = nextOffset;
      state();
      if (nextOffset < total) observer.observe(sentinel);
    } catch (error) {
      if (!disposed && list.isConnected)
        sentinel.replaceChildren(
          el("span", { class: "error" }, `Could not load more notes: ${error.message}`),
          button("Retry", loadMore),
        );
    } finally {
      loading = false;
      list.removeAttribute("aria-busy");
    }
  }
  state();
  list.append(sentinel);
  const detail = el("article", { class: "panel memory-detail" });
  if (target.id) {
    try {
      const note = await call("memory_get", { id: target.id });
      detail.append(
        link("← Back to notes", change({ note: null }), "memory-back"),
        el("div", { class: "eyebrow" }, `Memory · ${note.id}`),
        el(
          "div",
          { class: "row spread memory-title" },
          el("h2", {}, note.title),
          projects.items.some(
            (p) => p.full_name === note.project && ["owner", "editor"].includes(p.role),
          )
            ? link(
                "Edit note",
                memoryRoute(note.project, new URLSearchParams({ note: note.id, edit: "true" })),
                "button",
              )
            : null,
        ),
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
  const page = el(
    "section",
    { class: `memory-page${target.id ? " has-selection" : ""}` },
    el("div", { class: "eyebrow" }, "Knowledge"),
    el(
      "div",
      { class: "row spread" },
      el("h1", {}, "Memory"),
      el(
        "div",
        { class: "row" },
        link(
          "Add note",
          memoryRoute(target.project, new URLSearchParams({ create: "true" })),
          "button primary",
        ),
        link(
          target.project ? "Back to tasks" : "Projects",
          target.project ? projectRoute(target.project) : "/ui/",
          "button",
        ),
      ),
    ),
    el(
      "p",
      { class: "subtitle" },
      `${notes.count} notes · ${target.project || "All accessible projects"}`,
    ),
    filters,
    el("div", { class: "memory-layout" }, el("aside", { class: "memory-sidebar" }, list), detail),
  );
  page.mount = () => {
    if (nextOffset < total) observer.observe(sentinel);
  };
  page.dispose = () => {
    disposed = true;
    observer.disconnect();
  };
  return page;
}
