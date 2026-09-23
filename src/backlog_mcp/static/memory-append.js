import { el, field, modal } from "./dom.js";

export async function appendNote(note, { call, refresh, principal }) {
  const key = `backlog:memory-append:${document.documentElement.dataset.endpoint}:${principal}:${note.id}`;
  let draft = "";
  try {
    draft = localStorage.getItem(key) || "";
  } catch {}
  const text = el("textarea", { rows: 6, required: true, value: draft });
  const count = el("p", { class: "muted", role: "status" });
  function update() {
    const size = [...(note.body ? note.body + "\n\n" : ""), ...text.value].length;
    count.textContent = `${size} / 1024 characters after appending`;
    text.setCustomValidity(size > 1024 ? "The complete note must fit 1024 characters." : "");
    try {
      localStorage.setItem(key, text.value);
    } catch {}
  }
  text.oninput = update;
  update();
  const saved = await modal({
    title: "Append to note",
    description: `${note.title} — ${note.project}`,
    content: [field("Addition", text), count],
    action: "Append",
    onSubmit: async () => {
      await call("memory_append", { id: note.id, text: text.value });
      try {
        localStorage.removeItem(key);
      } catch {}
    },
  });
  if (saved) await refresh();
}
