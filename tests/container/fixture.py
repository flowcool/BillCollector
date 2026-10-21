"""Synthetic vault, portal and authenticated TLS proxy for the Compose lab.

Not deployment tooling. This fixture accepts only the fixed synthetic lab peers.
"""

import asyncio
import ssl

from aiohttp import BasicAuth, ClientSession, WSMsgType, web

visits = []


async def vault(request):
    if request.host != "127.0.0.1:8087":
        raise web.HTTPForbidden()
    if request.path == "/status":
        return web.json_response({"success": True, "data": {"template": {"status": "unlocked"}}})
    if request.path == "/object/item/synthetic user":
        return web.json_response({"success": True, "data": {"login": {
            "username": "synthetic-user", "password": "synthetic-password",
            "uris": [{"uri": "http://fixture:8080/"}],
        }}})
    if request.path == "/object/totp/synthetic user":
        raise web.HTTPBadRequest()
    raise web.HTTPNotFound()


async def portal(request):
    if request.path == "/invoice.pdf":
        return web.Response(body=b"%PDF-1.4\nSynthetic invoice\n%%EOF\n", headers={
            "Content-Type": "application/pdf",
            "Content-Disposition": 'attachment; filename="invoice.pdf"',
        })
    if request.path == "/metrics":
        return web.json_response({"visits": visits})
    if request.method == "POST":
        return web.Response(text="ok")  # synthetic ntfy endpoint
    if request.path != "/":
        raise web.HTTPNotFound()
    visits.append(request.cookies.get("persisted"))
    response = web.Response(text='<h1>Synthetic portal</h1><a href="/invoice.pdf">Download invoice</a>',
                            content_type="text/html")
    response.set_cookie("persisted", "yes", max_age=86400)
    return response


async def proxy(request):
    if request.headers.get("Authorization") != BasicAuth("operator", "synthetic-proxy-pass").encode():
        return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="synthetic-lab"'})
    browser = request.path.startswith("/browser/")
    path = str(request.rel_url)[len("/browser"):] if browser else str(request.rel_url)
    target = f"http://billcollector:{6080 if browser else 8000}{path}"
    async with ClientSession() as client:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            protocols = tuple(p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip())
            async with client.ws_connect(target, protocols=protocols, max_msg_size=16 * 1024 * 1024) as upstream:
                downstream = web.WebSocketResponse(protocols=protocols, max_msg_size=16 * 1024 * 1024)
                await downstream.prepare(request)

                async def forward(source, destination):
                    async for message in source:
                        if message.type == WSMsgType.BINARY:
                            await destination.send_bytes(message.data)
                        elif message.type == WSMsgType.TEXT:
                            await destination.send_str(message.data)
                        else:
                            break

                tasks = [asyncio.create_task(forward(upstream, downstream)),
                         asyncio.create_task(forward(downstream, upstream))]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                return downstream
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "authorization", "connection", "content-length")}
        async with client.request(request.method, target, headers=headers,
                                  data=await request.read(), allow_redirects=False) as response:
            excluded = ("transfer-encoding", "connection", "content-length", "content-encoding")
            return web.Response(status=response.status, body=await response.read(),
                                headers=[(k, v) for k, v in response.headers.items() if k.lower() not in excluded])


async def main():
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain("/certs/cert.pem", "/certs/key.pem")
    runners = []
    try:
        for handler, port, context in ((vault, 8087, None), (portal, 8080, None), (proxy, 8443, tls)):
            app = web.Application()
            app.router.add_route("*", "/{path:.*}", handler)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            runners.append(runner)
            await web.TCPSite(runner, "0.0.0.0", port, ssl_context=context).start()
        await asyncio.Event().wait()
    finally:
        for runner in runners:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
