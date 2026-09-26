"""Minimal health endpoint for a container supervisor; contains no client data."""

from aiohttp import web


async def start_health_server(port: int, status: dict):
    if not port:
        return None
    app = web.Application()

    async def health(_request):
        ready = bool(status.get("ready"))
        return web.json_response(
            {"status": "ok" if ready else "starting", "service": "by-vio-booking"},
            status=200 if ready else 503,
        )

    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
