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
export const projectRoute = (project) =>
  "#/p/" + project.split("/").map(encodeURIComponent).join("/");
export function route(hash = location.hash) {
  const [path, query = ""] = (hash || "#/").slice(1).split("?");
  const params = new URLSearchParams(query);
  if (path === "/") return { kind: "projects", params };
  if (path === "/t/new") return { kind: "new", params };
  const task = path.match(/^\/t\/(\d+)(\/edit)?$/);
  if (task) return { kind: task[2] ? "edit" : "task", id: Number(task[1]), params };
  const project = path.match(/^\/p\/([^/]+)\/([^/]+)$/);
  if (project)
    return {
      kind: "board",
      project: decodeURIComponent(project[1]) + "/" + decodeURIComponent(project[2]),
      params,
    };
  return { kind: "missing", params };
}
