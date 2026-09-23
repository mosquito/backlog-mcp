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
