import { el, button } from "./dom.js";

// Sources are consumed in order, so completed tasks never precede unloaded open tasks.
export function taskPagination({ sources, root, call, append, changed }) {
  const seen = new Set();
  let index = 0,
    loading = false,
    disposed = false;
  const streams = sources.map(({ args, page }) => ({
    args,
    buffer: page.items,
    offset: page.items.length,
    total: page.count,
  }));
  const sentinel = el("div", {
    class: "task-pagination",
    role: "status",
    "aria-live": "polite",
  });
  const observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) loadMore();
    },
    { root, rootMargin: "0px 0px 200px 0px" },
  );
  function flush() {
    while (index < streams.length) {
      const stream = streams[index];
      const fresh = stream.buffer.filter((task) => {
        if (seen.has(task.id)) return false;
        seen.add(task.id);
        return true;
      });
      append(fresh);
      stream.buffer = [];
      if (stream.offset < stream.total) break;
      index++;
    }
    const total = streams.reduce((sum, stream) => sum + stream.total, 0);
    changed(seen.size, total);
    sentinel.replaceChildren(
      el(
        "span",
        { class: "muted" },
        index < streams.length
          ? `${seen.size} of ${total} tasks · Scroll for more`
          : seen.size
            ? `${seen.size} tasks · All loaded`
            : "No tasks",
      ),
    );
  }
  async function loadMore() {
    if (loading || disposed || index >= streams.length || !sentinel.isConnected) return;
    loading = true;
    observer.unobserve(sentinel);
    root.setAttribute("aria-busy", "true");
    sentinel.replaceChildren(el("span", {}, "Loading more tasks…"));
    const stream = streams[index];
    try {
      const page = await call("task_list", {
        ...stream.args,
        offset: stream.offset,
      });
      if (disposed || !sentinel.isConnected) return;
      stream.offset += page.items.length;
      stream.total = page.items.length ? page.count : stream.offset;
      stream.buffer = page.items;
      flush();
    } catch (error) {
      if (!disposed && sentinel.isConnected)
        sentinel.replaceChildren(
          el("span", { class: "error" }, `Could not load more tasks: ${error.message}`),
          button("Retry", loadMore),
        );
      return;
    } finally {
      loading = false;
      root.removeAttribute("aria-busy");
    }
    if (!disposed && index < streams.length) observer.observe(sentinel);
  }
  flush();
  return {
    sentinel,
    mount() {
      if (!disposed && index < streams.length) observer.observe(sentinel);
    },
    dispose() {
      disposed = true;
      observer.disconnect();
    },
  };
}
