import { agentConfig } from "./agent-config.js";
import { loadCredentials } from "./credentials.js";
import { el, button, link } from "./dom.js";

const form = document.querySelector("#connection-form");
const endpoint = document.querySelector("#endpoint");
const computer = document.querySelector("#computer");
const method = document.querySelector("#method");
const username = document.querySelector("#username");
const secret = document.querySelector("#secret");
const output = document.querySelector("#setup-output");
const empty = document.querySelector("#setup-empty");
const error = document.querySelector("#connection-error");
const status = document.querySelector("#copy-status");
endpoint.value = new URL("/mcp", location.origin).href;
const computerWords = ["cedar", "maple", "birch", "pine", "oak", "elm", "willow", "aspen"];
computer.value =
  computerWords[Math.floor(Math.random() * computerWords.length)] +
  "-" +
  (Math.floor(Math.random() * 9999) + 1);
let selected = "claude",
  generated;
const guides = {
  claude: {
    commandTitle: "Add to Claude Code",
    commandHelp:
      "Run in bash or zsh. This registers Backlog for your user account. If “backlog” already exists, update its configuration instead.",
    configTitle: "Or merge into ~/.claude.json",
    configHelp: "Add the backlog entry under mcpServers. Keep your other servers and settings.",
    next: "Open Claude Code and run /mcp to check the connection.",
    docs: "https://code.claude.com/docs/en/mcp",
  },
  codex: {
    commandTitle: "Try in one Codex session",
    commandHelp:
      "Run in bash or zsh. These options apply to this session only. Use the configuration below to keep the connection.",
    configTitle: "Save in ~/.codex/config.toml",
    configHelp:
      "Add this section, or replace the existing mcp_servers.backlog section. Keep your other settings.",
    next: "Restart Codex after saving, then run /mcp to check the connection.",
    docs: "https://developers.openai.com/codex/mcp",
  },
  antigravity: {
    configTitle: "Merge into mcp_config.json",
    configHelp:
      "In the IDE, open MCP Servers → Manage MCP Servers → View raw config. Add backlog under mcpServers and keep your other servers. For the CLI, use ~/.gemini/config/mcp_config.json.",
    next: "Refresh the MCP servers list. In the CLI, use /mcp to check the connection.",
    docs: "https://antigravity.google/docs/mcp",
  },
};
function codeBlock(title, help, value) {
  const code = el("textarea", {
    class: "setup-code",
    readOnly: true,
    spellcheck: false,
    "aria-label": title,
    rows: Math.min(12, value.split("\n").length + 1),
  });
  code.value = value;
  const copy = button("Copy", async () => {
    try {
      await navigator.clipboard.writeText(value);
      status.textContent = "Copied to clipboard.";
    } catch {
      code.focus();
      code.select();
      status.textContent = "Copy is unavailable. The text is selected; press Ctrl+C or ⌘C.";
    }
  });
  copy.setAttribute("aria-label", `Copy ${title}`);
  return el(
    "section",
    { class: "setup-block" },
    el("div", { class: "section-heading" }, el("h3", {}, title), copy),
    el("p", { class: "muted" }, help),
    code,
  );
}
function render() {
  for (const tab of document.querySelectorAll("[data-agent]"))
    tab.setAttribute("aria-pressed", String(tab.dataset.agent === selected));
  status.textContent = "";
  if (!generated) return;
  const guide = guides[selected],
    data = generated[selected];
  output.replaceChildren(
    ...[
      el(
        "p",
        { class: "connection-note muted" },
        "Generated text contains your credentials. Keep it private.",
      ),
      data.command ? codeBlock(guide.commandTitle, guide.commandHelp, data.command) : null,
      codeBlock(guide.configTitle, guide.configHelp, data.config),
      el("p", { class: "setup-next" }, guide.next),
      link("Official setup documentation ↗", guide.docs),
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
method.onchange = () => {
  const basic = method.value === "basic",
    authenticated = method.value !== "none";
  document.querySelector("#username-field").hidden = !basic;
  document.querySelector("#secret-field").hidden = !authenticated;
  username.required = basic;
  secret.required = authenticated;
  secret.value = "";
  document.querySelector("#secret-label").textContent = basic ? "Password" : "Bearer token";
  clearOutput();
};
function restoreCredentials() {
  const credentials = loadCredentials(endpoint.value);
  if (!credentials) return;
  method.value = credentials.method;
  method.onchange();
  username.value = credentials.username;
  secret.value = credentials.secret;
}
restoreCredentials();
form.addEventListener("input", clearOutput);
form.onsubmit = (event) => {
  event.preventDefault();
  try {
    generated = agentConfig({
      endpoint: endpoint.value,
      computer: computer.value,
      method: method.value,
      username: username.value,
      secret: secret.value,
    });
    error.hidden = true;
    render();
  } catch (failure) {
    clearOutput();
    error.textContent = failure.message;
    error.hidden = false;
  }
};
for (const tab of document.querySelectorAll("[data-agent]"))
  tab.onclick = () => {
    selected = tab.dataset.agent;
    render();
  };

function fitCode() {
  for (const code of output.querySelectorAll("textarea")) {
    code.style.height = "auto";
    code.style.height = Math.min(420, code.scrollHeight + 2) + "px";
  }
}
window.addEventListener("resize", fitCode);
