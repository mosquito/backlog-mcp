import { agentConfig } from "./agent-config.js";
import { loadCredentials } from "./credentials.js";
import { el, button, link } from "./dom.js";

const form = document.querySelector("#connection-form");
const endpoint = document.querySelector("#endpoint");
const computer = document.querySelector("#computer");
const method = document.querySelector("#method");
const username = document.querySelector("#username");
const secret = document.querySelector("#secret");
const agent = document.querySelector("#agent");
const level = document.querySelector("#level");
const serverName = document.querySelector("#server-name");
const includeCredentials = document.querySelector("#include-credentials");
const output = document.querySelector("#setup-output");
const empty = document.querySelector("#setup-empty");
const error = document.querySelector("#connection-error");
const status = document.querySelector("#copy-status");
const levelKey = "mcp-console-agent-level:/console";
endpoint.value = new URL("/mcp", location.origin).href;
const computerWords = ["cedar", "maple", "birch", "pine", "oak", "elm", "willow", "aspen"];
computer.value =
  computerWords[Math.floor(Math.random() * computerWords.length)] +
  "-" +
  (Math.floor(Math.random() * 9999) + 1);
try {
  if (localStorage.getItem(levelKey) === "project") level.value = "project";
} catch {}
let generated;

function populateAgents() {
  const selected = agent.value;
  const variants = agentConfig({
    endpoint: endpoint.value, computer: "computer", method: "none", level: level.value,
  });
  agent.replaceChildren(...variants.map((variant) => el("option", { value: variant.id }, variant.label)));
  if (variants.some((variant) => variant.id === selected)) agent.value = selected;
}

function codeBlock(variant) {
  const code = el("textarea", {
    class: "setup-code", readOnly: true, spellcheck: false,
    "aria-label": `${variant.language.toUpperCase()} configuration`,
    rows: Math.min(12, variant.code.split("\n").length + 1),
  });
  code.value = variant.code;
  const copy = button("Copy configuration", async () => {
    try {
      await navigator.clipboard.writeText(variant.code);
      status.textContent = "Configuration copied.";
    } catch {
      code.focus();
      code.select();
      status.textContent = "Copy is unavailable. The text is selected; press Ctrl+C or ⌘C.";
    }
  });
  return el(
    "section", { class: "setup-block" },
    el("div", { class: "section-heading" }, el("h3", {}, variant.target), copy),
    el("p", { class: "muted" }, variant.instructions),
    code,
  );
}

function render() {
  status.textContent = "";
  if (!generated) return;
  const variant = generated.find((item) => item.id === agent.value);
  output.replaceChildren(
    ...[
      includeCredentials.checked && method.value !== "none"
        ? el("p", { class: "connection-note muted" }, "Generated text contains your credentials. Keep it private.")
        : null,
      codeBlock(variant),
      el("p", { class: "setup-next" }, variant.verify),
      link("Official setup documentation ↗", variant.link),
    ].filter(Boolean),
  );
  empty.hidden = true;
  output.hidden = false;
  requestAnimationFrame(fitCode);
}

function clearOutput() {
  generated = undefined;
  output.replaceChildren();
  output.hidden = true;
  empty.hidden = false;
  status.textContent = "";
  error.hidden = true;
}

function generate() {
  clearOutput();
  if (!form.checkValidity()) return;
  try {
    generated = agentConfig({
      endpoint: endpoint.value,
      computer: computer.value,
      method: method.value,
      username: username.value,
      secret: secret.value,
      name: serverName.value,
      level: level.value,
      includeCredentials: includeCredentials.checked,
    });
    render();
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
  }
}

function authenticationFields() {
  const basic = method.value === "basic", authenticated = method.value !== "none";
  document.querySelector("#username-field").hidden = !basic;
  document.querySelector("#secret-field").hidden = !authenticated;
  document.querySelector("#include-credentials-field").hidden = !authenticated;
  username.required = basic && includeCredentials.checked;
  secret.required = authenticated && includeCredentials.checked;
  document.querySelector("#secret-label").textContent = basic ? "Password" : "Bearer token";
}
method.onchange = () => {
  secret.value = "";
  authenticationFields();
  generate();
};
const credentials = loadCredentials(endpoint.value);
if (credentials) {
  method.value = credentials.method;
  username.value = credentials.username;
  secret.value = credentials.secret;
}
authenticationFields();
populateAgents();
generate();
form.addEventListener("input", generate);
form.onsubmit = (event) => {
  event.preventDefault();
  generate();
};
agent.onchange = render;
level.onchange = () => {
  try { localStorage.setItem(levelKey, level.value); } catch {}
  populateAgents();
  generate();
};
serverName.oninput = generate;
includeCredentials.onchange = () => {
  authenticationFields();
  generate();
};

function fitCode() {
  for (const code of output.querySelectorAll("textarea")) {
    code.style.height = "auto";
    code.style.height = Math.min(420, code.scrollHeight + 2) + "px";
  }
}
window.addEventListener("resize", fitCode);
