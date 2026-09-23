import { el } from "./dom.js";
import DOMPurify from "./vendor/purify.es.mjs";

let library;
let sequence = 0;
let queue = Promise.resolve();
const theme = matchMedia("(prefers-color-scheme: dark)");
function loadMermaid() {
  library ||= new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = new URL("./vendor/mermaid.min.js", import.meta.url).href;
    script.onload = () => resolve(globalThis.mermaid);
    script.onerror = () => {
      script.remove();
      library = null;
      reject(new Error("Could not load Mermaid"));
    };
    document.head.append(script);
  });
  return library;
}

class Diagram extends HTMLElement {
  connectedCallback() {
    if (!this.source) return;
    this.onTheme = () => this.render();
    theme.addEventListener("change", this.onTheme);
    this.render();
  }
  disconnectedCallback() {
    this.generation = (this.generation || 0) + 1;
    theme.removeEventListener("change", this.onTheme);
  }
  render() {
    const generation = (this.generation = (this.generation || 0) + 1);
    const source = this.source;
    const details = el(
      "details",
      { class: "diagram-source" },
      el("summary", {}, "Diagram source"),
      el("pre", {}, el("code", {}, source)),
    );
    const status = el("p", { class: "muted", role: "status" }, "Rendering diagram…");
    this.replaceChildren(status, details);
    this.setAttribute("aria-busy", "true");
    const current = () => this.isConnected && generation === this.generation;
    // Mermaid configuration is global. Serialize initialization and rendering across blocks.
    queue = queue.then(async () => {
      if (!current()) return;
      try {
        const mermaid = await loadMermaid();
        if (!current()) return;
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          suppressErrorRendering: true,
          theme: theme.matches ? "dark" : "default",
          fontFamily: "system-ui, sans-serif",
          htmlLabels: false,
          flowchart: { htmlLabels: false },
          layout: "dagre",
          maxTextSize: 50000,
          maxEdges: 500,
          secure: [
            "secure",
            "securityLevel",
            "startOnLoad",
            "suppressErrorRendering",
            "maxTextSize",
            "maxEdges",
            "htmlLabels",
            "flowchart",
            "theme",
            "themeVariables",
            "themeCSS",
            "fontFamily",
            "layout",
          ],
        });
        const { svg } = await mermaid.render(`backlog-diagram-${++sequence}`, source);
        if (!current()) return;
        const safe = DOMPurify.sanitize(svg, {
          USE_PROFILES: { svg: true, svgFilters: true },
          FORBID_TAGS: ["foreignObject"],
        });
        // An SVG image cannot execute scripts or turn diagram links into page navigation.
        const img = el("img", {
          class: "diagram-image",
          alt: "Mermaid diagram. Source follows below.",
          src: "data:image/svg+xml;charset=utf-8," + encodeURIComponent(safe),
        });
        const svgDocument = new DOMParser().parseFromString(safe, "image/svg+xml");
        const viewBox = svgDocument.documentElement
          .getAttribute("viewBox")
          ?.split(/[ ,]+/)
          .map(Number);
        if (viewBox?.length === 4 && Number.isFinite(viewBox[2]) && viewBox[2] > 0) {
          img.width = Math.ceil(viewBox[2]);
          svgDocument.documentElement.setAttribute("width", String(img.width));
          svgDocument.documentElement.setAttribute("height", String(Math.ceil(viewBox[3])));
          img.src =
            "data:image/svg+xml;charset=utf-8," +
            encodeURIComponent(new XMLSerializer().serializeToString(svgDocument));
        }
        status.replaceWith(
          el(
            "div",
            {
              class: "diagram-output",
              tabIndex: 0,
              role: "region",
              "aria-label": "Mermaid diagram; scroll to see more",
            },
            img,
          ),
        );
      } catch {
        if (!current()) return;
        status.textContent = "Could not render this diagram. Check its Mermaid syntax below.";
        status.className = "error";
        details.open = true;
      } finally {
        if (current()) this.removeAttribute("aria-busy");
      }
    });
  }
}
customElements.define("backlog-diagram", Diagram);
