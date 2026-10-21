"""Exercise native login, the real session iframe and RFB over an HTTPS proxy."""

import asyncio
import ssl

from aiohttp import BasicAuth, ClientSession, WSMsgType
from playwright.sync_api import sync_playwright


async def check_transport():
    tls = ssl.create_default_context(cafile="/certs/cert.pem")
    async with ClientSession() as client:
        for path in ("/", "/browser/vnc.html", "/browser/websockify"):
            async with client.get("https://localhost:8443" + path, ssl=tls) as response:
                assert response.status == 401
        async with client.ws_connect("https://localhost:8443/browser/websockify", ssl=tls,
                                     auth=BasicAuth("operator", "synthetic-proxy-pass"),
                                     protocols=("binary",)) as websocket:
            message = await websocket.receive(timeout=10)
            assert message.type == WSMsgType.BINARY
            assert message.data.startswith(b"RFB ")
    print("PASS anonymous proxy rejection and authenticated TLS RFB WebSocket", flush=True)


def main():
    asyncio.run(check_transport())
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            context = browser.new_context(
                ignore_https_errors=True,
                http_credentials={"username": "operator", "password": "synthetic-proxy-pass"},
            )
            page = context.new_page()
            page.goto("https://localhost:8443/login")
            page.locator('input[name="password"]').fill("synthetic-native-pass")
            confirm = page.locator('input[name="password_confirm"]')
            if confirm.count():
                confirm.fill("synthetic-native-pass")
            page.locator('button[type="submit"]').click()
            page.wait_for_url("https://localhost:8443/")
            page.goto("https://localhost:8443/runs/1")
            page.get_by_role("tab", name="noVNC", exact=True).click()
            iframe = page.locator("iframe[data-novnc-port]")
            iframe.wait_for(state="visible")
            assert "/browser/vnc.html?" in iframe.get_attribute("src")
            page.frame_locator("iframe[data-novnc-port]").locator("canvas").wait_for(state="visible", timeout=30000)
            context.close()
            print("PASS native first-password/login and real noVNC iframe canvas", flush=True)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
