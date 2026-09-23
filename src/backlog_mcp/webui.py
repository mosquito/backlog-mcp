"""Public browser subapplication; application data stays behind MCP."""

from pathlib import Path

from aiohttp import web

STATIC = Path(__file__).with_name("static")


def application() -> web.Application:
    """Build the UI subapplication for mounting at /ui."""
    app = web.Application()

    async def index(request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html")

    app.router.add_get("", index)
    app.router.add_get("/", index)
    app.router.add_static("/static/", STATIC)
    return app
