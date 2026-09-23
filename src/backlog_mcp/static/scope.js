import { el, field, select } from "./dom.js";

export function scopeKind(params) {
  const kind = params.get("scope");
  if (["all", "current", "computer", "project"].includes(kind)) return kind;
  return params.get("project") ? "project" : params.get("computer") ? "computer" : "all";
}
export function scopeArgs(params) {
  switch (scopeKind(params)) {
    case "project":
      if (!params.get("project")) throw new Error("Choose a project.");
      return { project: params.get("project") };
    case "current":
      return { all_projects: true };
    case "computer":
      if (!params.get("computer")?.trim()) throw new Error("Enter a computer name.");
      return { computer: params.get("computer").trim() };
    default:
      return { all_computers: true };
  }
}
export function scopeLabel(params, principal) {
  switch (scopeKind(params)) {
    case "project":
      return `Project: ${params.get("project")}`;
    case "current":
      return `Projects on this computer (${principal})`;
    case "computer":
      return `Projects on computer: ${params.get("computer")}`;
    default:
      return "All accessible projects on all computers";
  }
}
export function scopeControls(params, projects, { allowProject = true } = {}) {
  const kinds = allowProject
    ? ["all", "current", "computer", "project"]
    : ["all", "current", "computer"];
  const mode = select(kinds, scopeKind(params), { "aria-label": "Scope" });
  const labels = {
    all: "All computers",
    current: "This computer",
    computer: "Named computer",
    project: "One project",
  };
  for (const option of mode.options) option.textContent = labels[option.value];
  const project = select(
    [
      "",
      ...new Set([
        ...projects.map((p) => p.full_name),
        ...(params.get("project") ? [params.get("project")] : []),
      ]),
    ],
    params.get("project") || "",
    { "aria-label": "Scope project" },
  );
  project.options[0].textContent = "Choose a project";
  const computer = el("input", {
    value: params.get("computer") || "",
    "aria-label": "Scope computer",
    placeholder: "Computer name",
  });
  const projectField = field("Project", project),
    computerField = field("Computer", computer);
  const element = el(
    "div",
    { class: "scope-controls" },
    field("Scope", mode),
    projectField,
    computerField,
  );
  function sync() {
    projectField.hidden = project.disabled = mode.value !== "project";
    project.required = !project.disabled;
    computerField.hidden = computer.disabled = mode.value !== "computer";
    computer.required = !computer.disabled;
  }
  mode.onchange = sync;
  sync();
  return {
    element,
    apply(params) {
      for (const key of ["scope", "project", "computer", "offset"]) params.delete(key);
      params.set("scope", mode.value);
      if (mode.value === "project") params.set("project", project.value);
      if (mode.value === "computer") params.set("computer", computer.value.trim());
      return params;
    },
  };
}
