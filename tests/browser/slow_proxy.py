"""Test-only reverse proxy for tests/browser/cold_start_e2e.py (never deployed).

Holds the app's HTML pages and /_stcore/health for DELAY seconds — the way Azure's ingress holds
requests while a replica starts from zero — and passes static files and WebSockets through.

    uv run --no-project --with aiohttp python tests/browser/slow_proxy.py http://127.0.0.1:8702 8703 8
"""

import asyncio
import sys

import aiohttp
from aiohttp import WSMsgType, web

UP = sys.argv[1]
PORT = int(sys.argv[2])
DELAY = float(sys.argv[3])
APP_PAGES = {"/", "/chat", "/analysis", "/architecture", "/observability", "/_stcore/health"}
HOP = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "proxy-connection",
    "te",
    "trailer",
    "content-encoding",
    "content-length",
    "host",
}


async def handler(req):
    if req.headers.get("upgrade", "").lower() == "websocket":
        ws_c = web.WebSocketResponse(
            protocols=[
                p.strip()
                for p in req.headers.get("Sec-WebSocket-Protocol", "").split(",")
                if p.strip()
            ]
        )
        await ws_c.prepare(req)
        async with (
            aiohttp.ClientSession() as s,
            s.ws_connect(
                UP.replace("http", "ws") + req.path_qs,
                protocols=(ws_c.ws_protocol and [ws_c.ws_protocol]) or (),
                max_msg_size=0,
            ) as ws_u,
        ):

            async def pipe(a, b):
                async for m in a:
                    if m.type == WSMsgType.BINARY:
                        await b.send_bytes(m.data)
                    elif m.type == WSMsgType.TEXT:
                        await b.send_str(m.data)
                    else:
                        break

            await asyncio.gather(pipe(ws_c, ws_u), pipe(ws_u, ws_c), return_exceptions=True)
        return ws_c
    if req.method == "GET" and req.path in APP_PAGES:
        print(f"holding {req.path} for {DELAY}s", flush=True)
        await asyncio.sleep(DELAY)
    async with (
        aiohttp.ClientSession(auto_decompress=False) as s,
        s.request(
            req.method,
            UP + req.path_qs,
            headers={k: v for k, v in req.headers.items() if k.lower() not in HOP},
            data=await req.read(),
        ) as r,
    ):
        body = await r.read()
        return web.Response(
            status=r.status,
            body=body,
            headers={
                k: v for k, v in r.headers.items() if k.lower() not in HOP | {"content-length"}
            }
            | (
                {"Content-Encoding": r.headers["Content-Encoding"]}
                if "Content-Encoding" in r.headers
                else {}
            ),
        )


app = web.Application()
app.router.add_route("*", "/{tail:.*}", handler)
web.run_app(app, host="127.0.0.1", port=PORT, print=None)
