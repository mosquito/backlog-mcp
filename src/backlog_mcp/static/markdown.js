import { taskRoute } from "./dom.js";
import { marked } from "./vendor/marked.esm.js";
import DOMPurify from "./vendor/purify.es.mjs";

export function markdown(source = "") {
  const fragment = DOMPurify.sanitize(marked.parse(source, { gfm: true, async: false }), {
    RETURN_DOM_FRAGMENT: true,
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style", "form", "iframe", "object"],
    FORBID_ATTR: ["style"],
  });
  const walker = document.createTreeWalker(fragment, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) {
    if (!walker.currentNode.parentElement?.closest("a,code,pre")) nodes.push(walker.currentNode);
  }
  for (const node of nodes) {
    const pattern = /\B#(\d+)\b/g;
    let last = 0,
      match;
    const replacement = document.createDocumentFragment();
    while ((match = pattern.exec(node.textContent))) {
      replacement.append(node.textContent.slice(last, match.index));
      const link = document.createElement("a");
      link.href = taskRoute(match[1]);
      link.textContent = match[0];
      replacement.append(link);
      last = match.index + match[0].length;
    }
    if (last) {
      replacement.append(node.textContent.slice(last));
      node.replaceWith(replacement);
    }
  }
  for (const link of fragment.querySelectorAll("a[href]")) {
    const href = link.getAttribute("href");
    const task = href.match(/^#(?:\/t\/)?([1-9]\d*)$/);
    if (task) link.href = taskRoute(task[1]);
    else if (!href.startsWith("/ui/#")) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
  }
  return fragment;
}
