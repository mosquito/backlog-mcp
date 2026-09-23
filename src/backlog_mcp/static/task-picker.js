import { el } from "./dom.js";

let nextId = 0;
/** Task completion with explicit async states and keyboard selection. */
export function taskPicker({ search, onSelect }) {
  const id = `task-picker-${++nextId}`;
  const input = el("input", {
    placeholder: "Search by task title…",
    autocomplete: "off",
    role: "combobox",
    "aria-label": "Find related task",
    "aria-autocomplete": "list",
    "aria-controls": id,
    "aria-expanded": "false",
  });
  const message = el("p", { class: "completion-message", role: "status" });
  const list = el("div", {
    id,
    role: "listbox",
    "aria-label": "Matching tasks",
    class: "completion-list",
  });
  const popup = el("div", { class: "completion-popup", hidden: true }, message, list);
  const selected = el("p", { class: "completion-selection", role: "status", hidden: true });
  const element = el(
    "div",
    { class: "task-picker" },
    el("label", {}, "Find a task", input),
    popup,
    selected,
  );
  let timer,
    generation = 0,
    items = [],
    active = -1;
  function close() {
    clearTimeout(timer);
    generation++;
    popup.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    input.removeAttribute("aria-busy");
    items = [];
    active = -1;
    list.replaceChildren();
  }
  function state(text) {
    popup.hidden = false;
    input.setAttribute("aria-expanded", "true");
    input.removeAttribute("aria-activedescendant");
    message.textContent = text;
    list.replaceChildren();
    items = [];
    active = -1;
  }
  function highlight() {
    [...list.children].forEach((node, index) =>
      node.setAttribute("aria-selected", String(index === active)),
    );
    const node = list.children[active];
    if (node) {
      input.setAttribute("aria-activedescendant", node.id);
      // Scroll only the results, not the surrounding dialog or input.
      if (node.offsetTop < list.scrollTop) list.scrollTop = node.offsetTop;
      if (node.offsetTop + node.offsetHeight > list.scrollTop + list.clientHeight)
        list.scrollTop = node.offsetTop + node.offsetHeight - list.clientHeight;
    }
  }
  function choose(index) {
    const task = items[index];
    if (!task) return;
    close();
    input.value = task.title;
    selected.textContent = `Selected #${task.id} · ${task.project}`;
    selected.hidden = false;
    onSelect(task);
  }
  function schedule() {
    close();
    selected.hidden = true;
    onSelect(null);
    const query = input.value.trim();
    if (!query) return;
    const current = generation;
    state("Searching…");
    input.setAttribute("aria-busy", "true");
    timer = setTimeout(async () => {
      try {
        const found = await search(query);
        if (current !== generation) return;
        items = found;
        input.removeAttribute("aria-busy");
        message.textContent = items.length
          ? `${items.length} ${items.length === 1 ? "match" : "matches"} · ↑ ↓ to browse, Enter to select`
          : "No matching tasks. Try another title or enter a task ID below.";
        list.replaceChildren(
          ...items.map((task, index) =>
            el(
              "div",
              {
                id: `${id}-${index}`,
                role: "option",
                "aria-selected": "false",
                class: "completion-option",
                onpointerdown: (event) => event.preventDefault(),
                onclick: () => choose(index),
              },
              el("span", { class: "completion-id" }, `#${task.id}`),
              el(
                "span",
                { class: "completion-text" },
                el("strong", { title: task.title }, task.title),
                el("small", {}, `${task.project} · ${task.status}`),
              ),
            ),
          ),
        );
      } catch (error) {
        if (current !== generation) return;
        input.removeAttribute("aria-busy");
        state(`Search failed: ${error.message}. Edit the query to retry.`);
      }
    }, 250);
  }
  input.oninput = (event) => {
    if (!event.isComposing) schedule();
  };
  input.addEventListener("compositionend", schedule);
  input.onkeydown = (event) => {
    if (event.isComposing) return;
    if (event.key === "Escape" && !popup.hidden) {
      event.preventDefault();
      event.stopPropagation();
      close();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (popup.hidden) {
        schedule();
        return;
      }
      if (!items.length) return;
      active =
        active < 0
          ? event.key === "ArrowDown"
            ? 0
            : items.length - 1
          : (active + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      highlight();
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (!popup.hidden && items.length) choose(active < 0 ? 0 : active);
    } else if (event.key === "Tab") close();
  };
  input.onblur = close;
  return {
    element,
    close,
    clearSelection() {
      selected.hidden = true;
      input.value = "";
      close();
    },
  };
}
