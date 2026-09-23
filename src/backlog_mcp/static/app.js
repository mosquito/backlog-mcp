import { MCP, HttpError } from "./mcp.js";
import { markdown } from "./markdown.js";
import {
  el,
  button,
  link,
  select,
  field,
  date,
  projectRoute,
  taskRoute,
  route,
  modal,
} from "./dom.js";
import { editor } from "./editor.js";
import { taskPicker } from "./task-picker.js";

const content = document.querySelector("#content");
const endpoint = document.documentElement.dataset.endpoint;
const credentialKey = `backlog:auth:${endpoint}`;
const statuses = ["todo", "doing", "blocked", "done", "cancelled"];
const priorities = ["urgent", "high", "normal", "low"];
const relations = [
  "part_of",
  "contains",
  "depends_on",
  "blocks",
  "caused_by",
  "causes",
  "related_to",
];
let client,
  identity,
  project = "",
  generation = 0,
  searchGeneration = 0;
let selectedResult = 0;
const actions = {
  call,
  notify,
  navigate,
  markdown,
  get principal() {
    return identity?.principal;
  },
};
function navigate(href) {
  const url = new URL(href, location.href);
  if (url.href !== location.href) history.pushState(null, "", url);
  render();
}
function notify(error) {
  if (error.name === "AbortError") return;
  const toast = document.querySelector("#toast");
  toast.textContent = error.message || String(error);
  toast.hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => {
    toast.hidden = true;
  }, 8000);
}
async function call(name, args = {}) {
  const active = client;
  try {
    return await active.call(name, args);
  } catch (error) {
    if (error instanceof HttpError && error.status === 401 && active === client) {
      identity = null;
      generation++;
      showLogin(error);
    }
    throw error;
  }
}
function operation(fn) {
  return async () => {
    try {
      await fn();
    } catch (error) {
      notify(error);
    }
  };
}
function credentials() {
  return sessionStorage.getItem(credentialKey) || localStorage.getItem(credentialKey) || "";
}
function clearCredentials() {
  sessionStorage.removeItem(credentialKey);
  localStorage.removeItem(credentialKey);
}
async function connect(authorization = credentials()) {
  client?.close();
  client = new MCP(endpoint, authorization);
  identity = await client.call("whoami");
  document.querySelector("#principal").textContent = identity.principal;
  document.querySelector("#logout").hidden = false;
  await render();
}
function showLogin(error, reportError = true) {
  document.querySelector("#principal").textContent = "";
  document.querySelector("#logout").hidden = true;
  document.querySelector("#project-name").textContent = "";
  document.querySelector("#search-dialog").close();
  const methods = error?.schemes?.length ? [...new Set(error.schemes)] : ["basic", "bearer"];
  const method = select(methods, methods[0], { name: "method" });
  const username = el("input", { name: "username", autocomplete: "username", required: true });
  const secret = el("input", {
    name: "secret",
    type: "password",
    autocomplete: "current-password",
    required: true,
  });
  const userField = field("Username", username),
    secretField = field("Password", secret);
  const remember = el("input", { type: "checkbox", name: "remember" });
  const message = el(
    "p",
    { class: "error", role: "alert" },
    error && reportError ? error.message : "",
  );
  const submit = el("button", { type: "submit", class: "primary" }, "Sign in");
  const form = el(
    "form",
    { class: "panel login" },
    el("div", { class: "eyebrow" }, "Your workspace"),
    el("h1", {}, "Sign in to Backlog"),
    el("p", { class: "subtitle" }, "Projects, tasks and the work in between."),
    field("Method", method),
    userField,
    secretField,
    el("label", { class: "inline" }, remember, "Remember me"),
    message,
    submit,
  );
  function changeMethod() {
    userField.hidden = method.value !== "basic";
    username.required = !userField.hidden;
    secretField.firstChild.textContent = method.value === "basic" ? "Password" : "Bearer token";
  }
  method.onchange = changeMethod;
  changeMethod();
  form.onsubmit = async (event) => {
    event.preventDefault();
    submit.disabled = true;
    message.textContent = "";
    try {
      if (method.value === "basic" && username.value.includes(":"))
        throw new Error("The username cannot contain a colon.");
      const bytes = new TextEncoder().encode(`${username.value}:${secret.value}`);
      const authorization =
        method.value === "basic"
          ? "Basic " + btoa(Array.from(bytes, (b) => String.fromCharCode(b)).join(""))
          : "Bearer " + secret.value.trim();
      await connect(authorization);
      clearCredentials();
      (remember.checked ? localStorage : sessionStorage).setItem(credentialKey, authorization);
    } catch (failure) {
      message.textContent = failure.message;
    } finally {
      submit.disabled = false;
    }
  };
  content.replaceChildren(form);
}
document.querySelector("#logout").onclick = () => {
  clearCredentials();
  client?.close();
  identity = null;
  project = "";
  generation++;
  searchGeneration++;
  showLogin();
};

async function render() {
  let target = route();
  if (target.redirect) {
    history.replaceState(null, "", target.redirect);
    target = route();
  }
  if (!identity) return;
  const current = ++generation;
  let selectedProject = "";
  content.replaceChildren(el("p", { class: "muted" }, "Loading…"));
  try {
    let page;
    if (target.kind === "projects") {
      page = await projects(target);
    } else if (target.kind === "board") {
      selectedProject = target.project;
      page = await board(target);
    } else if (target.kind === "task") {
      const task = await call("task_get", { id: target.id });
      selectedProject = task.project;
      page = await detail(task);
    } else if (target.kind === "new" || target.kind === "edit") {
      const task = target.kind === "edit" ? await call("task_get", { id: target.id }) : null;
      selectedProject = task?.project || target.params.get("project") || "";
      if (!selectedProject) throw new Error("Choose a project before creating a task.");
      page = editor(task, selectedProject, actions);
    } else page = el("div", {}, el("h1", {}, "Page not found"), link("Back to projects", "/ui/"));
    if (current !== generation || !identity) return;
    project = selectedProject;
    content.replaceChildren(page);
    document.querySelector("#project-name").textContent = project;
  } catch (error) {
    if (current !== generation) return;
    content.replaceChildren(
      el(
        "div",
        { class: "panel" },
        el("h1", {}, "Could not load this page"),
        el("p", { class: "error" }, error.message),
        button("Try again", render),
        link("Projects", "/ui/"),
      ),
    );
  }
}
async function projects(target) {
  const archived = target.params.get("archived") === "true";
  const data = await call("project_list", { all_computers: true, archived });
  const toggle = el("input", {
    type: "checkbox",
    checked: archived,
    onchange: () => navigate(toggle.checked ? "/ui/?archived=true" : "/ui/"),
  });
  return el(
    "section",
    {},
    el("div", { class: "eyebrow" }, "Workspace"),
    el("h1", {}, "Your projects"),
    el(
      "div",
      { class: "row spread" },
      el("p", { class: "subtitle" }, `${data.count} projects · Pick up where you left off.`),
      el("label", { class: "inline" }, toggle, "Include archived"),
    ),
    el("div", { class: "projects" }, ...data.items.map((p) => linkNode(p))),
  );
}
function linkNode(p) {
  return el(
    "a",
    { class: "project", href: projectRoute(p.full_name) },
    el("div", { class: "eyebrow" }, p.role + (p.archived ? " · archived" : "")),
    el("h2", {}, p.name),
    el("p", {}, p.full_name),
    el("p", {}, p.description || "No description"),
    el(
      "div",
      { class: "row spread" },
      el("strong", {}, `${p.open_tasks} open`),
      el("span", { class: "muted" }, `${p.total_tasks} total →`),
    ),
  );
}
async function board(target) {
  const params = target.params;
  const filters = { project: target.project };
  if (params.get("tag")) filters.tag = params.get("tag");
  if (params.get("priority")) filters.priority = params.get("priority");
  const [open, done, summary] = await Promise.all([
    call("task_list", { ...filters, status: ["todo", "doing", "blocked"], limit: 500 }),
    call("task_list", { ...filters, status: ["done"], order: "recent", limit: 50 }),
    call("project_board", { project: target.project }),
  ]);
  const tag = el("input", {
    placeholder: "Filter by tag",
    value: filters.tag || "",
    "aria-label": "Tag filter",
  });
  const priority = select(["", ...priorities], filters.priority || "", {
    "aria-label": "Priority filter",
  });
  priority.options[0].text = "All priorities";
  const apply = () => {
    const p = new URLSearchParams();
    if (tag.value.trim()) p.set("tag", tag.value.trim());
    if (priority.value) p.set("priority", priority.value);
    navigate(projectRoute(target.project, p));
  };
  const heading = el(
    "div",
    {},
    el("div", { class: "eyebrow" }, "Project board"),
    el(
      "div",
      { class: "row spread" },
      el("h1", {}, summary.project.name),
      el(
        "div",
        { class: "row" },
        button("Refresh", render),
        link(
          "+ New task",
          "/ui/?view=new&project=" + encodeURIComponent(target.project),
          "button primary",
        ),
      ),
    ),
    el(
      "p",
      { class: "subtitle" },
      `${summary.tasks.open} open · ${summary.tasks.done} done · ${summary.tasks.total} total`,
    ),
    el(
      "form",
      {
        class: "row filters",
        onsubmit: (e) => {
          e.preventDefault();
          apply();
        },
      },
      tag,
      priority,
      el("button", { type: "submit" }, "Apply filters"),
    ),
  );
  const columns = el("div", { class: "board" });
  const tasks = [...open.items, ...done.items];
  for (const status of statuses.slice(0, 4)) {
    const items = tasks.filter((t) => t.status === status);
    const column = el(
      "section",
      { class: "column", "data-status": status },
      el("h2", {}, status, el("span", { class: "count" }, String(items.length))),
    );
    const stack = el("div", { class: "stack" });
    column.append(stack);
    for (const task of items) {
      const card = el(
        "article",
        { class: "card", draggable: true, "data-task": task.id },
        el(
          "div",
          { class: "card-meta" },
          `#${task.id}`,
          el("span", { class: `priority ${task.priority}` }, task.priority),
        ),
        link(task.title, taskRoute(task.id)),
        el("div", {}, ...(task.tags || []).map((t) => el("span", { class: "tag" }, t))),
        el(
          "small",
          { class: "muted", title: task.assigned || "Unassigned" },
          task.assigned || "Unassigned",
        ),
      );
      card.ondragstart = (event) => {
        event.dataTransfer.setData("text/plain", String(task.id));
        event.dataTransfer.effectAllowed = "move";
      };
      stack.append(card);
    }
    column.ondragover = (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
    };
    column.ondrop = async (event) => {
      event.preventDefault();
      const id = Number(event.dataTransfer.getData("text/plain"));
      const task = tasks.find((t) => t.id === id);
      const card = columns.querySelector(`[data-task="${id}"]`);
      if (!task || !card || task.status === status || card.dataset.saving) return;
      const parent = card.parentNode,
        next = card.nextSibling;
      card.dataset.saving = "true";
      stack.prepend(card);
      try {
        await call("task_update", { id, status });
        await render();
      } catch (error) {
        parent.insertBefore(card, next);
        notify(error);
      } finally {
        delete card.dataset.saving;
      }
    };
    columns.append(column);
  }
  return el(
    "section",
    { class: "board-page" },
    heading,
    columns,
    el(
      "p",
      { class: "muted" },
      `Showing up to 500 open tasks and the 50 most recently changed completed tasks. Refresh to load updates.`,
    ),
  );
}
async function detail(task) {
  const panel = el("section", { class: "detail" });
  const status = select(
    statuses.includes(task.status) ? statuses : [...statuses, task.status],
    task.status,
    { "aria-label": "Status" },
  );
  const priority = select(priorities, task.priority, { "aria-label": "Priority" });
  for (const [control, key] of [
    [status, "status"],
    [priority, "priority"],
  ])
    control.onchange = async () => {
      control.disabled = true;
      try {
        await call("task_update", { id: task.id, [key]: control.value });
        await render();
      } catch (error) {
        control.value = task[key];
        notify(error);
      } finally {
        control.disabled = false;
      }
    };
  panel.append(
    link("← " + task.project, projectRoute(task.project)),
    el("div", { class: "eyebrow", style: "margin-top:24px" }, `Task #${task.id}`),
    el("h1", {}, task.title),
    el(
      "div",
      { class: "task-actions" },
      field("Status", status),
      field("Priority", priority),
      link("Edit task", taskRoute(task.id, true), "button"),
      button(
        "Move",
        operation(async () => {
          const result = await call("project_list", { all_computers: true });
          const chooser = select(
            result.items.filter((p) => p.role !== "reader").map((p) => p.full_name),
            task.project,
          );
          const moved = await modal({
            title: "Move task",
            description: task.title,
            content: [field("Destination project", chooser)],
            action: "Move task",
            onSubmit: () => call("task_move", { id: task.id, project: chooser.value }),
          });
          if (moved) await render();
        }),
      ),
      button(
        "Delete",
        operation(async () => {
          const removed = await modal({
            title: "Delete task?",
            description: task.title,
            content: [
              el("p", {}, "The task will be marked as deleted and removed from the board."),
            ],
            action: "Delete task",
            danger: true,
            onSubmit: () => call("task_delete", { id: task.id }),
          });
          if (removed) navigate(projectRoute(task.project));
        }),
        "danger",
      ),
    ),
    el(
      "p",
      { class: "muted" },
      `Assigned: ${task.assigned || "Unassigned"} · Created ${date(task.created_at)} by ${task.created_by} · Updated ${date(task.updated_at)} by ${task.updated_by}${task.closed_at ? ` · Closed ${date(task.closed_at)}` : ""}`,
    ),
    el("div", {}, ...(task.tags || []).map((t) => el("span", { class: "tag" }, t))),
    el("div", { class: "panel markdown" }, markdown(task.body || "No description.")),
  );
  const links = el(
    "ul",
    { class: "links" },
    ...(task.links || []).map((item) =>
      el(
        "li",
        {},
        el(
          "div",
          { class: "row" },
          el("span", { class: "tag" }, item.relation),
          link(`#${item.task} ${item.title}`, taskRoute(item.task)),
          el("small", { class: "muted" }, item.status),
          button(
            "Remove",
            operation(async () => {
              const removed = await modal({
                title: "Remove relation?",
                description: item.title,
                content: [
                  el("p", {}, `Remove the ${item.relation} relation? Both tasks will be kept.`),
                ],
                action: "Remove relation",
                danger: true,
                onSubmit: () =>
                  call("task_unlink", {
                    task: task.id,
                    target: item.task,
                    relation: item.relation,
                  }),
              });
              if (removed) await render();
            }),
            "quiet",
          ),
        ),
        el("small", { class: "muted" }, item.reason || ""),
      ),
    ),
  );
  const relation = select(relations, "related_to", { "aria-label": "Relation type" });
  const target = el("input", {
    type: "number",
    min: 1,
    required: true,
    placeholder: "Task ID",
    "aria-label": "Target task ID",
  });
  const reason = el("input", { placeholder: "Reason (optional)", "aria-label": "Relation reason" });
  const picker = taskPicker({
    search: async (query) => {
      const found = await call("search", {
        project: task.project,
        all_computers: true,
        query,
        kinds: ["tasks"],
        limit: 9,
      });
      return found.tasks.filter((item) => item.id !== task.id).slice(0, 8);
    },
    onSelect: (item) => {
      target.value = item ? item.id : "";
    },
  });
  target.oninput = () => picker.clearSelection();
  const addRelation = button("Add relation", async () => {
    const added = await modal({
      title: "Add relation",
      description: "Find a task or enter its ID, then choose how it relates to this task.",
      content: [
        picker.element,
        field("Task ID", target),
        field("Relation", relation),
        field("Reason (optional)", reason),
      ],
      action: "Add relation",
      onSubmit: () =>
        call("task_link", {
          task: task.id,
          target: Number(target.value),
          relation: relation.value,
          reason: reason.value,
        }),
    });
    picker.close();
    if (added) await render();
  });
  panel.append(
    el(
      "section",
      { class: "panel relations" },
      el("div", { class: "section-heading" }, el("h2", {}, "Relations"), addRelation),
      task.links?.length ? links : el("p", { class: "empty-state" }, "No relations yet."),
    ),
  );
  const commentBody = el("textarea", {
    placeholder: "Write a comment in Markdown…",
    "aria-label": "Comment",
  });
  const preview = el("div", { class: "comment-preview markdown", hidden: true });
  const commentError = el("p", { class: "error", role: "alert", hidden: true });
  const textTab = button("Write", () => changeCommentView(false));
  const previewTab = button("Preview", () => changeCommentView(true));
  function changeCommentView(showPreview) {
    if (showPreview) preview.replaceChildren(markdown(commentBody.value));
    commentBody.hidden = showPreview;
    preview.hidden = !showPreview;
    textTab.setAttribute("aria-pressed", String(!showPreview));
    previewTab.setAttribute("aria-pressed", String(showPreview));
  }
  const commentForm = el(
    "form",
    { class: "comment-form" },
    el("label", { htmlFor: "comment-body" }, "Add a comment"),
    el("div", { class: "editor-tabs" }, textTab, previewTab),
    commentBody,
    preview,
    commentError,
    el(
      "div",
      { class: "form-actions" },
      el("small", { class: "muted" }, "Markdown supported"),
      el("button", { type: "submit", class: "primary" }, "Add comment"),
    ),
  );
  commentBody.id = "comment-body";
  changeCommentView(false);
  commentForm.onsubmit = async (e) => {
    e.preventDefault();
    if (!commentBody.value.trim()) {
      changeCommentView(false);
      commentError.textContent = "Write a comment before sending.";
      commentError.hidden = false;
      commentBody.focus();
      return;
    }
    const submit = commentForm.querySelector('[type="submit"]');
    submit.disabled = true;
    commentError.hidden = true;
    try {
      const added = await call("task_comment_add", { task_id: task.id, body: commentBody.value });
      await render();
      document.getElementById(`comment-${added.id}`)?.scrollIntoView({ block: "center" });
    } catch (error) {
      commentError.textContent = error.message;
      commentError.hidden = false;
    } finally {
      submit.disabled = false;
    }
  };
  const comments = (task.comments || []).map((comment) => {
    const system = comment.kind !== "comment";
    return el(
      "article",
      { class: system ? "comment system-event" : "comment", id: `comment-${comment.id}` },
      el(
        "div",
        { class: "comment-avatar", "aria-hidden": "true" },
        system ? "↳" : comment.principal.slice(0, 1).toUpperCase(),
      ),
      el(
        "div",
        { class: "comment-content" },
        el(
          "div",
          { class: "comment-heading" },
          el("strong", {}, comment.principal),
          system
            ? el("span", { class: "tag" }, comment.kind === "status" ? "Status changed" : "Moved")
            : null,
          el(
            "time",
            { dateTime: new Date(comment.created_at * 1000).toISOString() },
            date(comment.created_at),
          ),
        ),
        el("div", { class: "markdown" }, markdown(comment.body)),
      ),
    );
  });
  panel.append(
    el(
      "section",
      { class: "panel activity" },
      el(
        "div",
        { class: "section-heading" },
        el("h2", {}, "Discussion & activity"),
        el("span", { class: "count" }, String(comments.length)),
      ),
      commentForm,
      el("div", { class: "timeline" }, ...comments),
      comments.length
        ? null
        : el("p", { class: "empty-state" }, "No activity yet. Start the conversation above."),
    ),
  );
  return panel;
}
const dialog = document.querySelector("#search-dialog");
const query = document.querySelector("#search-query"),
  all = document.querySelector("#search-all"),
  results = document.querySelector("#search-results");
function openSearch() {
  if (!identity) return;
  all.checked = !project;
  all.disabled = !project;
  dialog.showModal();
  query.focus();
  search();
}
document.querySelector("#search-open").onclick = openSearch;
document.querySelector("#search-close").onclick = () => dialog.close();
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    openSearch();
  }
});
let searchTimer;
query.setAttribute("role", "combobox");
query.setAttribute("aria-autocomplete", "list");
query.setAttribute("aria-controls", "search-results");
query.setAttribute("aria-expanded", "false");
results.setAttribute("role", "listbox");
results.setAttribute("aria-label", "Search results");
function searchState(text, loading = false) {
  results.replaceChildren(el("p", { class: "search-state", role: "status" }, text));
  query.removeAttribute("aria-activedescendant");
  query.setAttribute("aria-expanded", "false");
  query.setAttribute("aria-busy", String(loading));
}
function scheduleSearch() {
  searchGeneration++;
  clearTimeout(searchTimer);
  if (!query.value.trim()) {
    searchState("Start typing to find tasks and comments.");
    return;
  }
  searchState("Searching…", true);
  searchTimer = setTimeout(search, 250);
}
query.oninput = (event) => {
  if (!event.isComposing) scheduleSearch();
};
query.addEventListener("compositionend", scheduleSearch);
all.onchange = () => {
  clearTimeout(searchTimer);
  search();
};
dialog.addEventListener("close", () => {
  searchGeneration++;
  clearTimeout(searchTimer);
  query.removeAttribute("aria-activedescendant");
  query.setAttribute("aria-expanded", "false");
  query.setAttribute("aria-busy", "false");
});
async function search() {
  const current = ++searchGeneration;
  if (!query.value.trim()) {
    searchState("Start typing to find tasks and comments.");
    return;
  }
  searchState("Searching…", true);
  try {
    const args = { query: query.value, kinds: ["tasks", "comments"], limit: 15 };
    if (project) args.project = project;
    if (all.checked || !project) args.all_computers = true;
    const found = await call("search", args);
    if (current !== searchGeneration || !identity || !dialog.open) return;
    query.setAttribute("aria-busy", "false");
    results.replaceChildren();
    for (const kind of ["tasks", "comments"]) {
      if (!found[kind].length) continue;
      results.append(el("h3", { role: "presentation" }, kind === "tasks" ? "Tasks" : "Comments"));
      for (const item of found[kind])
        results.append(
          el(
            "a",
            {
              class: "search-result",
              id: `search-${kind}-${item.id}`,
              role: "option",
              "aria-selected": "false",
              href: taskRoute(item.task_id || item.id),
              onclick: () => dialog.close(),
            },
            el(
              "strong",
              {},
              kind === "tasks" ? `#${item.id} ${item.title}` : `Comment on #${item.task_id}`,
            ),
            el(
              "small",
              { class: "search-meta" },
              [item.project, item.status].filter(Boolean).join(" · "),
            ),
            el("small", {}, (item.snippet || item.body || "").slice(0, 220)),
          ),
        );
    }
    if (!results.children.length)
      searchState("No matches. Try another query or search all projects.");
    query.setAttribute("aria-expanded", String(Boolean(results.querySelector("a"))));
    selectedResult = 0;
    highlight(false);
  } catch (error) {
    if (current === searchGeneration)
      searchState(`Search failed: ${error.message}. Edit the query to retry.`);
  }
}
function highlight(scroll = true) {
  const links = [...results.querySelectorAll("a")];
  links.forEach((a, i) => {
    a.classList.toggle("selected", i === selectedResult);
    a.setAttribute("aria-selected", String(i === selectedResult));
  });
  const active = links[selectedResult];
  if (active) {
    query.setAttribute("aria-activedescendant", active.id);
    if (scroll) {
      if (active.offsetTop < results.scrollTop) results.scrollTop = active.offsetTop;
      if (active.offsetTop + active.offsetHeight > results.scrollTop + results.clientHeight)
        results.scrollTop = active.offsetTop + active.offsetHeight - results.clientHeight;
    }
  }
}
query.onkeydown = (e) => {
  if (e.isComposing) return;
  const links = [...results.querySelectorAll("a")];
  if (!links.length) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    selectedResult =
      (selectedResult + (e.key === "ArrowDown" ? 1 : -1) + links.length) % links.length;
    highlight();
  }
  if (e.key === "Enter") {
    e.preventDefault();
    links[selectedResult]?.click();
  }
};
document.addEventListener("click", (event) => {
  if (
    event.defaultPrevented ||
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    event.altKey
  )
    return;
  const anchor = event.target.closest("a[href]");
  if (!anchor || anchor.target || anchor.hasAttribute("download")) return;
  const url = new URL(anchor.href);
  if (url.origin !== location.origin || !["/ui", "/ui/"].includes(url.pathname)) return;
  event.preventDefault();
  navigate(url.href);
});
window.addEventListener("popstate", render);
window.addEventListener("hashchange", render);
connect().catch((error) => {
  if (error instanceof HttpError && error.status === 401) showLogin(error, Boolean(credentials()));
  else {
    showLogin();
    notify(error);
  }
});
