import { el, field, modal, projectRoute } from "./dom.js";

export async function createProject({ call, navigate, principal }) {
  const name = el("input", { required: true, maxLength: 200 });
  const description = el("textarea", { rows: 3 });
  const language = el("input", { placeholder: "No preference" });
  const root = el("input", {
    placeholder: "Leave empty for a project without local files",
    pattern: "(/.*)?",
  });
  let created;
  const saved = await modal({
    title: "New project",
    description: `Binding belongs to ${principal}. Enter an absolute path on that computer, not a browser folder. Leave it empty if no local path is needed.`,
    content: [
      field("Name", name),
      field("Description", description),
      field("Preferred language", language),
      field("Directory", root),
    ],
    action: "Create project",
    onSubmit: async () => {
      created = await call("project_save", {
        root: root.value.trim(),
        name: name.value.trim(),
        description: description.value,
        preferred_language: language.value.trim() || null,
      });
    },
  });
  if (saved) navigate(projectRoute(created.full_name));
}

export async function editProject(project, current, { call }) {
  const name = el("input", { required: true, value: current.name });
  const description = el("textarea", { rows: 3, value: current.description || "" });
  const language = el("input", {
    value: current.preferred_language || "",
    placeholder: "No preference",
  });
  let saved;
  await modal({
    title: "Edit project",
    action: "Save changes",
    content: [
      field("Name", name),
      field("Description", description),
      field("Preferred language", language),
    ],
    onSubmit: async () => {
      saved = await call("project_save", {
        project,
        name: name.value.trim(),
        description: description.value,
        preferred_language: language.value.trim() || null,
      });
    },
  });
  return saved;
}

export async function addBinding(project, { call, principal }) {
  const root = el("input", { placeholder: "No local path", pattern: "(/.*)?" });
  return modal({
    title: "Add computer binding",
    description: `This binding belongs to ${principal}. Enter an absolute path on that computer. Other bindings are kept. An empty path means no local files.`,
    content: [field("Directory", root)],
    action: "Add binding",
    onSubmit: () => call("project_save", { project, root: root.value.trim() }),
  });
}
