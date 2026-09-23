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
  memoryRoute,
  taskRoute,
  route,
  modal,
} from "./dom.js";
import { editor } from "./editor.js";
import { taskPicker } from "./task-picker.js";
import { taskTable } from "./task-table.js";
import { taskPagination } from "./task-pagination.js";
import { memoryPage } from "./memory.js";
import { commentComposer } from "./comment-composer.js";
import { breadcrumbs } from "./breadcrumbs.js";
import { createProject } from "./project-form.js";
import { assignmentControl, assignmentsPage } from "./assignments.js";
import { projectSettings } from "./project-settings.js";

const content = document.querySelector("#content");
const endpoint = document.documentElement.dataset.endpoint;
const pageLoading = document.querySelector("#page-loading");
let displayedUrl = "";
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
  refresh: render,
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
  client = new MCP(endpoint, authorization, async (request) => {
    if (request.requestedSchema?.properties?.ok?.type !== "boolean")
      return { action: "decline" };
    const ok = await modal({
      title: "Confirm deletion", description: request.message,
      action: "Delete permanently", danger: true,
    });
    return { action: "accept", content: { ok } };
  });
  identity = await client.call("whoami");
  document.querySelector("#principal").textContent = identity.principal;
  document.querySelector("#logout").hidden = false;
  document.querySelector("#section-switch").hidden = false;
  await render();
}
function finishStartup() {
  document.documentElement.removeAttribute("data-starting");
  document.querySelector("header").inert = false;
}
function showLogin(error, reportError = true) {
  finishStartup();
  content.inert = false;
  content.removeAttribute("aria-busy");
  pageLoading.hidden = true;
  displayedUrl = "";
  content.firstElementChild?.dispose?.();
  document.querySelector("#principal").textContent = "";
  document.querySelector("#logout").hidden = true;
  document.querySelector("#section-switch").hidden = true;
  document.querySelector("#breadcrumbs").replaceChildren();
  document.querySelector("#breadcrumbs").hidden = true;
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
  const targetUrl = location.href;
  content.inert = true;
  content.setAttribute("aria-busy", "true");
  pageLoading.hidden = false;
  let selectedProject = "";
  content.firstElementChild?.dispose?.();
  try {
    let page;
    if (target.kind === "projects") {
      page = await projects(target);
    } else if (target.kind === "memory") {
      selectedProject = target.project;
      page = await memoryPage(target, actions);
    } else if (target.kind === "board") {
      selectedProject = target.project;
      page = await board(target);
    } else if (target.kind === "assigned") {
      page = await assignmentsPage(target, actions);
    } else if (target.kind === "settings") {
      selectedProject = target.project;
      page = await projectSettings(target.project, actions);
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
    if (current !== generation || !identity) {
      page.dispose?.();
      return;
    }
    project = selectedProject;
    content.replaceChildren(page);
    if (displayedUrl !== targetUrl) window.scrollTo(0, 0);
    displayedUrl = targetUrl;
    page.mount?.();
    document.querySelector("#breadcrumbs").replaceChildren(breadcrumbs(target, project));
    document.querySelector("#breadcrumbs").hidden = false;
    document.querySelector("#memory-open").href = memoryRoute(project);
    document.querySelector("#tasks-open").href = project ? projectRoute(project) : "/ui/";
    for (const section of ["tasks", "memory"]) {
      const control = document.querySelector(`#${section}-open`);
      if ((target.kind === "memory") === (section === "memory"))
        control.setAttribute("aria-current", "page");
      else control.removeAttribute("aria-current");
    }
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
  } finally {
    if (current === generation) {
      finishStartup();
      content.inert = false;
      content.removeAttribute("aria-busy");
      pageLoading.hidden = true;
    }
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
    el("div", { class: "row spread" }, el("h1", {}, "Your projects"),
      button("New project", () => createProject(actions), "primary")),
    el(
      "div",
      { class: "row spread" },
      el("p", { class: "subtitle" }, `${data.count} projects · Pick up where you left off.`),
      link("My assignments", "/ui/?view=assigned", "button"),
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
function boardSection(title, data = { count: 0, items: [] }, explanation) {
  return el("details", { class: "board-summary" },
    el("summary", {}, `${title} (${data.count})`),
    el("p", { class: "muted" }, explanation),
    el("p", { class: "muted" }, `${data.items.length} of ${data.count} shown`),
    data.items.length ? el("ul", {}, ...data.items.map(task =>
      el("li", {}, link(`#${task.id} ${task.title}`, taskRoute(task.id)),
        ` · ${task.status} · ${task.priority}`))) : el("p", {}, "No tasks."));
}
async function board(target) {
  const params = target.params;
  const viewKey = `backlog:board-view:${endpoint}:${identity.principal}`;
  let preferredView;
  try {
    preferredView = localStorage.getItem(viewKey);
  } catch {
    preferredView = null;
  }
  const tableView = (params.get("view") || preferredView) === "table";
  const queryText = params.get("q") || "";
  const selectedStatus = [...statuses, "deleted"].includes(params.get("status"))
    ? params.get("status")
    : "";
  const visibleStatuses = selectedStatus ? [selectedStatus] : statuses.slice(0, 4);
  const groups = selectedStatus
    ? [[selectedStatus]]
    : tableView
      ? [["todo", "doing", "blocked"], ["done"]]
      : visibleStatuses.map((status) => [status]);
  async function loadSources(text, priorityValue) {
    if (text) {
      const found = await call("search", {
        project: target.project,
        query: text,
        kinds: ["tasks"],
        status: visibleStatuses,
        limit: 100,
      });
      const tasks = found.tasks.filter((task) => !priorityValue || task.priority === priorityValue);
      const matches = tableView
        ? [tasks]
        : groups.map((group) => tasks.filter((task) => group.includes(task.status)));
      return matches.map((items) => ({ args: {}, page: { items, count: items.length } }));
    }
    const requests = groups.map((status) => ({
      project: target.project,
      ...(priorityValue ? { priority: priorityValue } : {}),
      status,
      order: ["done", "cancelled", "deleted"].includes(status[0]) ? "recent" : "backlog",
      limit: 50,
    }));
    return Promise.all(
      requests.map(async (args) => ({ args, page: await call("task_list", args) })),
    );
  }
  const [sources, summary] = await Promise.all([
    loadSources(queryText, params.get("priority")),
    call("project_board", { project: target.project }),
  ]);
  const counts = el("span", { class: "muted" });
  const quick = el("input", {
    type: "search",
    placeholder: "Search tasks",
    value: queryText,
    "aria-label": "Search tasks",
    autocomplete: "off",
  });
  const priority = select(["", ...priorities], params.get("priority") || "", {
    "aria-label": "Priority filter",
  });
  priority.options[0].text = "All priorities";
  const statusFilter = select(["", ...statuses, "deleted"], selectedStatus, {
    "aria-label": "Task status",
  });
  statusFilter.options[0].text = "Open and done";
  for (const option of [...statusFilter.options].slice(1))
    option.text = option.value[0].toUpperCase() + option.value.slice(1);
  statusFilter.onchange = () => {
    const p = new URLSearchParams(location.search);
    p.delete("status");
    if (statusFilter.value) p.set("status", statusFilter.value);
    navigate(projectRoute(target.project, p));
  };
  const deletedParams = new URLSearchParams(params);
  deletedParams.set("status", "deleted");
  deletedParams.delete("q");
  deletedParams.delete("priority");
  const apply = () => schedule(0);
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
      `${summary.tasks.open} open · ${summary.tasks.done} done · ${summary.tasks.total} total · `,
      link(`Deleted (${summary.tasks.deleted || 0})`, projectRoute(target.project, deletedParams)),
    ),
    el("div", { class: "board-summaries" },
      boardSection("Ready", summary.ready, "Unassigned todo/doing tasks with no unfinished direct dependencies. Up to 10 tasks are listed."),
      boardSection("My active tasks", summary.claimed, `Open tasks assigned to ${identity.principal}, across all markers. Up to 10 tasks are listed.`)),
    el(
      "form",
      {
        class: "row filters",
        onsubmit: (e) => {
          e.preventDefault();
          apply();
        },
      },
      quick,
      statusFilter,
      priority,
      el("button", { type: "submit" }, "Search"),
    ),
  );
  const switchView = (view) => {
    try {
      localStorage.setItem(viewKey, view);
    } catch {
      notify(new Error("The browser could not save the view preference."));
    }
    const p = new URLSearchParams(location.search);
    p.set("view", view);
    navigate(projectRoute(target.project, p));
  };
  const viewButton = (title, view, selected) => {
    const control = button(title, () => switchView(view));
    control.setAttribute("aria-pressed", String(selected));
    return control;
  };
  const toolbar = el(
    "div",
    { class: "board-toolbar" },
    el(
      "div",
      { class: "view-switch", role: "group", "aria-label": "Task view" },
      viewButton("Kanban", "kanban", !tableView),
      viewButton("Table", "table", tableView),
      button("Settings", () =>
        navigate(projectRoute(target.project, new URLSearchParams({ view: "settings" }))),
      ),
    ),
    counts,
    link("My assignments", "/ui/?view=assigned"),
    tableView
      ? el("span", { class: "muted table-scroll-hint" }, "Scroll the table to see all columns")
      : null,
  );
  heading.querySelector(".filters").append(toolbar);
  const message = el("p", { class: "muted", role: "status", "aria-live": "polite" });
  let results = taskResults(sources, tableView, counts, Boolean(queryText), visibleStatuses);
  const panel = el("section", { class: "board-page" }, heading, message, results);
  let revision = 0,
    timer,
    disposed = false;
  function schedule(delay = 250) {
    const current = ++revision;
    clearTimeout(timer);
    results.dispose();
    const p = new URLSearchParams(location.search);
    p.delete("tag");
    p.delete("q");
    p.delete("priority");
    if (quick.value.trim()) p.set("q", quick.value.trim());
    if (priority.value) p.set("priority", priority.value);
    history.replaceState(null, "", projectRoute(target.project, p));
    timer = setTimeout(() => refresh(current), delay);
  }
  async function refresh(current) {
    message.textContent = "Searching…";
    results.setAttribute("aria-busy", "true");
    try {
      const next = await loadSources(quick.value.trim(), priority.value);
      if (disposed || current !== revision) return;
      const replacement = taskResults(
        next,
        tableView,
        counts,
        Boolean(quick.value.trim()),
        visibleStatuses,
      );
      results.replaceWith(replacement);
      results = replacement;
      results.mount();
      message.textContent = "";
    } catch (error) {
      if (!disposed && current === revision) {
        message.replaceChildren(
          el("span", { class: "error" }, error.message),
          button("Retry", () => schedule(0)),
        );
        results.removeAttribute("aria-busy");
      }
    }
  }
  quick.oninput = (event) => {
    if (!event.isComposing) schedule();
  };
  quick.addEventListener("compositionstart", () => {
    revision++;
    clearTimeout(timer);
  });
  quick.addEventListener("compositionend", () => schedule());
  priority.onchange = () => schedule(0);
  panel.mount = () => results.mount();
  panel.dispose = () => {
    disposed = true;
    revision++;
    clearTimeout(timer);
    results.dispose();
  };
  return panel;
}
function taskResults(sources, tableView, counts, searching, visibleStatuses) {
  const tasks = [];
  const pagers = [];
  const loaded = new Map();
  function count(key, shown, total) {
    loaded.set(key, { shown, total });
    counts.textContent = `${[...loaded.values()].reduce((n, item) => n + item.shown, 0)} of ${[...loaded.values()].reduce((n, item) => n + item.total, 0)} tasks shown`;
  }
  function finish(...children) {
    const page = el("section", { class: "board-results" }, ...children, note);
    page.mount = () => pagers.forEach((pager) => pager.mount());
    page.dispose = () => pagers.forEach((pager) => pager.dispose());
    return page;
  }
  const note = el(
    "p",
    { class: "muted" },
    searching
      ? "Up to 100 search matches. Refine your query to narrow the results."
      : "Tasks load as you scroll. Refresh to load updates.",
  );
  if (tableView) {
    const table = taskTable([]);
    const pager = taskPagination({
      sources,
      root: table,
      call,
      append: (items) => table.appendTasks(items),
      changed: (shown, total) => count("table", shown, total),
    });
    table.append(pager.sentinel);
    pagers.push(pager);
    return finish(table);
  }
  const columns = el("div", { class: "board" });
  if (visibleStatuses.length === 1) columns.classList.add("single-status");
  for (const [index, status] of visibleStatuses.entries()) {
    const counter = el("span", { class: "count" });
    const column = el(
      "section",
      { class: "column", "data-status": status },
      el("h2", {}, status, counter),
    );
    const stack = el("div", { class: "stack" });
    column.append(stack);
    function append(items) {
      tasks.push(...items);
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
        stack.insertBefore(card, stack.querySelector(".task-pagination"));
      }
    }
    const pager = taskPagination({
      sources: [sources[index]],
      root: stack,
      call,
      append,
      changed: (shown, total) => {
        counter.textContent = `${shown} / ${total}`;
        count(status, shown, total);
      },
    });
    stack.append(pager.sentinel);
    pagers.push(pager);
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
  return finish(columns);
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
    link(
      "← " + task.project,
      projectRoute(
        task.project,
        task.status === "deleted"
          ? new URLSearchParams({ status: "deleted" })
          : new URLSearchParams(),
      ),
    ),
    el("div", { class: "eyebrow", style: "margin-top:24px" }, `Task #${task.id}`),
    el("h1", {}, task.title),
    task.status === "deleted"
      ? el(
          "p",
          { class: "muted" },
          "This task is deleted. Its description and history are preserved.",
        )
      : null,
    el(
      "div",
      { class: "task-actions" },
      field("Status", status),
      field("Priority", priority),
      await assignmentControl(task, actions),
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
      task.status === "deleted"
        ? null
        : button(
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
        min_score: 0.12,
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
      task.links?.length ? links : null,
    ),
  );
  const composer = commentComposer({
    taskId: task.id,
    draftKey: `backlog:comment-draft:${JSON.stringify([endpoint, identity.principal, task.id])}`,
    call,
    markdown,
    onAdded: async (added) => {
      if (!panel.isConnected) return;
      await render();
      document.getElementById(`comment-${added.id}`)?.scrollIntoView({ block: "center" });
    },
  });
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
      composer,
      el("div", { class: "timeline" }, ...comments),
      comments.length
        ? null
        : el("p", { class: "empty-state" }, "No activity yet. Start the conversation above."),
    ),
  );
  return panel;
}
const dialog = document.querySelector("#search-dialog");
const searchKinds = [...dialog.querySelectorAll('[name="search-kind"]')];
const searchLabels = {
  tasks: "Tasks",
  comments: "Comments",
  memory: "Memory",
  projects: "Projects",
};
const query = document.querySelector("#search-query"),
  all = document.querySelector("#search-all"),
  results = document.querySelector("#search-results");
let searchAllPreference = false;
const searchPreferenceKey = () => `backlog:search:${endpoint}:${identity.principal}`;
function searchPlaceholder() {
  query.placeholder = all.checked ? "Search across all projects" : `Search in ${project}`;
}
function openSearch() {
  if (!identity) return;
  let saved;
  try {
    saved = JSON.parse(localStorage.getItem(searchPreferenceKey()));
  } catch {
    saved = null;
  }
  searchAllPreference = saved?.all === true;
  for (const input of searchKinds)
    input.checked = Array.isArray(saved?.kinds) ? saved.kinds.includes(input.value) : true;
  all.checked = !project || searchAllPreference;
  all.disabled = !project;
  searchPlaceholder();
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
  if (!searchKinds.some((input) => input.checked)) {
    searchState("Select at least one content type to search.");
    return;
  }
  if (!query.value.trim()) {
    searchState("Start typing to search the selected content.");
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
  if (!all.disabled) searchAllPreference = all.checked;
  searchPlaceholder();
  try {
    localStorage.setItem(
      searchPreferenceKey(),
      JSON.stringify({
        all: searchAllPreference,
        kinds: searchKinds.filter((input) => input.checked).map((input) => input.value),
      }),
    );
  } catch {
    notify(new Error("The browser could not save search preferences."));
  }
  search();
};
for (const input of searchKinds) input.onchange = all.onchange;
dialog.addEventListener("close", () => {
  searchGeneration++;
  clearTimeout(searchTimer);
  query.removeAttribute("aria-activedescendant");
  query.setAttribute("aria-expanded", "false");
  query.setAttribute("aria-busy", "false");
});
async function search() {
  const current = ++searchGeneration;
  const kinds = searchKinds.filter((input) => input.checked).map((input) => input.value);
  if (!kinds.length) {
    searchState("Select at least one content type to search.");
    return;
  }
  if (!query.value.trim()) {
    searchState("Start typing to search the selected content.");
    return;
  }
  searchState("Searching…", true);
  try {
    const args = { query: query.value, kinds, limit: 15, min_score: 0.12 };
    if (all.checked || !project) args.all_computers = true;
    else args.project = project;
    const found = await call("search", args);
    if (current !== searchGeneration || !identity || !dialog.open) return;
    query.setAttribute("aria-busy", "false");
    results.replaceChildren();
    for (const kind of kinds) {
      if (!found[kind].length) continue;
      results.append(el("h3", { role: "presentation" }, searchLabels[kind]));
      for (const item of found[kind])
        results.append(
          el(
            "a",
            {
              class: "search-result",
              id: `search-${kind}-${item.id}`,
              role: "option",
              "aria-selected": "false",
              href:
                kind === "memory"
                  ? memoryRoute(item.project, new URLSearchParams({ note: item.id }))
                  : kind === "projects"
                    ? projectRoute(item.full_name)
                    : taskRoute(item.task_id || item.id),
              onclick: () => dialog.close(),
            },
            el(
              "strong",
              {},
              kind === "tasks"
                ? `#${item.id} ${item.title}`
                : kind === "comments"
                  ? `Comment on #${item.task_id}`
                  : item.title || item.name,
            ),
            el(
              "small",
              { class: "search-meta" },
              [item.project || item.full_name, item.status, item.shared ? "Shared" : ""]
                .filter(Boolean)
                .join(" · "),
            ),
            el("small", {}, (item.snippet || item.body || item.description || "").slice(0, 220)),
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
