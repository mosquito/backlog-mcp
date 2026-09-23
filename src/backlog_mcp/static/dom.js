export function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else if (name === "class") node.className = value;
    else if (name in node) node[name] = value;
    else node.setAttribute(name, value);
  }
  node.append(...children.filter((child) => child !== null && child !== undefined));
  return node;
}
export const button = (text, action, className = "") =>
  el("button", { type: "button", class: className, onclick: action }, text);
export const link = (text, href, className = "") => el("a", { href, class: className }, text);
export const select = (values, value, attributes = {}) => {
  const node = el("select", attributes, ...values.map((v) => el("option", { value: v }, v)));
  node.value = value;
  return node;
};
export const field = (text, control) => el("label", {}, text, control);
export const date = (value) => (value ? new Date(value * 1000).toLocaleString() : "—");
export const projectRoute = (project, filters = new URLSearchParams()) => {
  const params = new URLSearchParams(filters);
  params.set("project", project);
  return "/ui/?" + params;
};
export const taskRoute = (id, edit = false) => `/ui/${edit ? "?view=edit" : ""}#${id}`;
export function route(href = location.href) {
  const url = new URL(href, "https://backlog.invalid/ui/");
  const params = url.searchParams;
  // Redirect saved links from the original hash router.
  if (url.hash.startsWith("#/")) {
    const [path, query = ""] = url.hash.slice(1).split("?");
    const old = new URLSearchParams(query);
    const task = path.match(/^\/t\/(\d+)(\/edit)?$/);
    if (task) return { redirect: taskRoute(task[1], Boolean(task[2])) };
    if (path === "/t/new") {
      old.set("view", "new");
      return { redirect: "/ui/?" + old };
    }
    const project = path.match(/^\/p\/([^/]+)\/([^/]+)$/);
    if (project) {
      try {
        return {
          redirect: projectRoute(
            decodeURIComponent(project[1]) + "/" + decodeURIComponent(project[2]),
            old,
          ),
        };
      } catch {
        return { kind: "missing", params };
      }
    }
    if (path === "/") return { redirect: "/ui/" + (old.size ? "?" + old : "") };
  }
  const task = url.hash.match(/^#([1-9]\d*)$/);
  if (task && Number.isSafeInteger(Number(task[1])))
    return { kind: params.get("view") === "edit" ? "edit" : "task", id: Number(task[1]), params };
  if (url.hash) return { kind: "missing", params };
  if (params.get("view") === "new") return { kind: "new", params };
  if (params.get("project")) return { kind: "board", project: params.get("project"), params };
  return { kind: "projects", params };
}

/** Accessible modal with one async action and an inline error state. */
export function modal({
  title,
  description = "",
  content = [],
  action = "Confirm",
  danger = false,
  onSubmit = async () => {},
}) {
  return new Promise((resolve) => {
    const heading = el("h2", { id: "modal-title" }, title);
    const error = el("p", { class: "error modal-error", role: "alert", hidden: true });
    const submit = el(
      "button",
      { type: "submit", class: danger ? "primary destructive" : "primary" },
      action,
    );
    const cancel = button("Cancel", () => dialog.close());
    const form = el(
      "form",
      { class: "modal-form" },
      el(
        "div",
        { class: "modal-heading" },
        heading,
        description ? el("p", { class: "muted" }, description) : null,
      ),
      el("div", { class: "modal-content" }, ...content),
      error,
      el("div", { class: "modal-actions" }, cancel, submit),
    );
    const dialog = el("dialog", { class: "action-dialog", "aria-labelledby": "modal-title" }, form);
    let confirmed = false,
      pending = false;
    form.onsubmit = async (event) => {
      event.preventDefault();
      if (pending) return;
      pending = true;
      submit.disabled = cancel.disabled = true;
      error.hidden = true;
      try {
        await onSubmit();
        confirmed = true;
        dialog.close();
      } catch (failure) {
        error.textContent = failure.message;
        error.hidden = false;
      } finally {
        pending = false;
        submit.disabled = cancel.disabled = false;
      }
    };
    dialog.addEventListener("cancel", (event) => {
      if (pending) event.preventDefault();
    });
    dialog.addEventListener(
      "close",
      () => {
        dialog.remove();
        resolve(confirmed);
      },
      { once: true },
    );
    document.body.append(dialog);
    dialog.showModal();
    (danger ? cancel : form.querySelector("input,select,textarea") || cancel).focus();
  });
}
