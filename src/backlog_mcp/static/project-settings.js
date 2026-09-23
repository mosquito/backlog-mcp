import { editProject, addBinding } from "./project-form.js";
import { el, button, field, select, projectRoute, modal } from "./dom.js";

export async function projectSettings(project, actions) {
  const { call, navigate, principal, notify } = actions;
  const [summary, access] = await Promise.all([
    call("project_board", { project }),
    call("project_members", { project }),
  ]);
  const title = el("h1", {}, summary.project.name);
  const description = el("p", { class: "muted" }, summary.project.description || "No description.");
  const language = el(
    "p",
    {},
    `Preferred language: ${summary.project.preferred_language || "No preference"}`,
  );
  const owner = summary.project.role === "owner";
  const members = el("div", { class: "project-members" });
  const bindings = el("div", { class: "project-bindings" });
  let disposed = false;
  function show(data) {
    if (disposed) return;
    members.replaceChildren(
      ...data.items.map((member) =>
        el(
          "div",
          { class: "member-row" },
          el("strong", {}, member.principal),
          el("span", { class: "tag" }, member.role),
          owner && member.role !== "owner" ? button("Change role", () => share(member)) : null,
        ),
      ),
    );
    bindings.replaceChildren(
      ...(data.roots || []).map((binding) => {
        const colon = binding.indexOf(":");
        const actor = binding.slice(0, colon);
        const at = actor.lastIndexOf("@");
        return el(
          "div",
          { class: "binding-row" },
          el(
            "div",
            {},
            el("span", { class: "muted" }, "Principal"),
            el("strong", {}, decodeURIComponent(actor.slice(0, at))),
          ),
          el(
            "div",
            {},
            el("span", { class: "muted" }, "Computer"),
            el("span", {}, decodeURIComponent(actor.slice(at + 1))),
          ),
          el(
            "div",
            {},
            el("span", { class: "muted" }, "Path"),
            el("code", {}, binding.slice(colon + 1) || "No local path"),
          ),
        );
      }),
    );
    if (!bindings.childElementCount)
      bindings.append(el("p", { class: "muted" }, "No computer bindings yet."));
  }
  async function share(member) {
    const username = el("input", {
      required: true,
      value: member?.principal || "",
      placeholder: "Username or user@computer",
      readOnly: Boolean(member),
    });
    const role = select(["reader", "editor"], member?.role || "reader");
    await modal({
      title: member ? "Change project role" : "Share project",
      description:
        "Readers can view the project. Editors can change its content. The user must have an account on this server.",
      content: [field("Principal", username), field("Role", role)],
      action: member ? "Save role" : "Grant access",
      onSubmit: async () => {
        const data = await call("project_share", {
          project,
          principal: username.value.trim(),
          role: role.value,
        });
        show(data);
      },
    });
  }
  function changeView(view) {
    try {
      localStorage.setItem(
        `backlog:board-view:${document.documentElement.dataset.endpoint}:${principal}`,
        view,
      );
    } catch {
      /* Explicit navigation still works without browser storage. */
    }
    navigate(projectRoute(project, new URLSearchParams({ view })));
  }
  const active = button("Settings", () => {});
  active.setAttribute("aria-pressed", "true");
  const page = el(
    "section",
    { class: "project-settings" },
    el("div", { class: "eyebrow" }, "Project settings"),
    title,
    el(
      "nav",
      { class: "view-switch", "aria-label": "Project view" },
      button("Kanban", () => changeView("kanban")),
      button("Table", () => changeView("table")),
      active,
    ),
    el(
      "section",
      { class: "panel" },
      el(
        "div",
        { class: "row spread" },
        el("h2", {}, "Project info"),
        summary.project.role !== "reader"
          ? button("Edit project", async () => {
              const saved = await editProject(project, summary.project, actions);
              if (!saved || disposed) return;
              Object.assign(summary.project, saved);
              title.textContent = saved.name;
              description.textContent = saved.description || "No description.";
              language.textContent = `Preferred language: ${saved.preferred_language || "No preference"}`;
            })
          : null,
      ),
      el("p", {}, el("strong", {}, project)),
      description,
      language,
      el("p", {}, `Your role: ${summary.project.role}`),
      summary.project.archived ? el("p", {}, "Archived project") : null,
    ),
    el(
      "section",
      { class: "panel" },
      el(
        "div",
        { class: "row spread" },
        el("h2", {}, "Members"),
        owner ? button("Share project", () => share(), "primary") : null,
      ),
      members,
      owner ? null : el("p", { class: "muted" }, "Only the project owner can manage access."),
    ),
    el(
      "section",
      { class: "panel" },
      el(
        "div",
        { class: "row spread" },
        el("h2", {}, "Computer bindings"),
        button("Add binding", async () => {
          if (await addBinding(project, actions)) {
            try {
              show(await call("project_members", { project }));
            } catch (error) {
              notify(error);
            }
          }
        }),
      ),
      bindings,
    ),
  );
  page.dispose = () => {
    disposed = true;
  };
  show(access);
  return page;
}
