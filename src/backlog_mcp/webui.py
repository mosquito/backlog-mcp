"""Public browser subapplication; application data stays behind MCP."""

from pathlib import Path

from aiohttp import web
from aiohttp_tiny_mcp.console import Console

STATIC = Path(__file__).with_name("static")
MODULES = (
    "app.js",
    "credentials.js",
    "mcp.js",
    "markdown.js",
    "dom.js",
    "editor.js",
    "editor-claim.js",
    "task-picker.js",
    "task-table.js",
    "task-browser.js",
    "task-pagination.js",
    "memory.js",
    "memory-editor.js",
    "memory-append.js",
    "comment-composer.js",
    "breadcrumbs.js",
    "project-settings.js",
    "project-form.js",
    "assignments.js",
    "statistics.js",
    "scope.js",
    "diagrams.js",
    "vendor/marked.esm.js",
    "vendor/purify.es.mjs",
)
# Send hints with the file headers, before the browser discovers imports in the body.
INDEX_LINK = ", ".join(
    [f"</ui/static/{name}>; rel=preload; as=style" for name in ("app.css", "markdown.css")]
    + [
        f"</ui/static/vendor/highlight-{theme}.css>; rel=preload; as=style; "
        f'media="(prefers-color-scheme: {theme})"'
        for theme in ("light", "dark")
    ]
    + ["</ui/static/vendor/highlight.min.js>; rel=preload; as=script"]
    + [f"</ui/static/{name}>; rel=modulepreload" for name in MODULES]
)


def application() -> web.Application:
    """Build the UI subapplication for mounting at /ui."""
    app = web.Application()
    configurations = Console().render("agent-config.js")

    async def index(request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html", headers={"Link": INDEX_LINK})

    async def agent_config(request: web.Request) -> web.Response:
        return web.Response(text=configurations, content_type="application/javascript")

    app.router.add_get("", index)
    app.router.add_get("/", index)
    app.router.add_get("/static/console-agent-config.js", agent_config)
    app.router.add_static("/static/", STATIC)
    return app
