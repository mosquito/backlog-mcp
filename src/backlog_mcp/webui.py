"""Public browser subapplication; application data stays behind MCP."""

from html import escape
from pathlib import Path

from aiohttp import web

STATIC = Path(__file__).with_name("static")


def application(endpoint: str) -> web.Application:
    """Build the UI subapplication for mounting at /ui."""
    app = web.Application()
    template = (STATIC / "index.html").read_text(encoding="utf-8")
    template = template.replace("{{ENDPOINT}}", escape(endpoint, quote=True))

    async def index(request: web.Request) -> web.Response:
        return web.Response(text=template, content_type="text/html")

    app.router.add_get("", index)
    app.router.add_get("/", index)
    app.router.add_static("/static/", STATIC)
    return app
