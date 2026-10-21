"""Exercise generated noVNC URLs in Chromium without a portal or vault."""

import pytest
from playwright.sync_api import sync_playwright

from billcollector.ui.session import novnc_src_script


@pytest.mark.parametrize("prefix, expected", [
    ("", "http://collector.example:6080/vnc.html?autoconnect=true"),
    ("/browser", "https://collector.example/browser/vnc.html?autoconnect=true&path=browser%2Fwebsockify"),
    ("/browser/", "https://collector.example/browser/vnc.html?autoconnect=true&path=browser%2Fwebsockify"),
])
def test_browser_resolves_http_and_websocket_paths(monkeypatch, prefix, expected):
    monkeypatch.setenv("BC_NOVNC_PATH", prefix)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.fulfill(body="<html><body></body></html>"))
            page.goto("https://collector.example/")
            page.set_content('<iframe data-novnc-port="6080"></iframe>' + novnc_src_script())
            assert page.locator("iframe").get_attribute("src") == expected
        finally:
            browser.close()


@pytest.mark.parametrize("prefix", ["https://other.example", "//other.example", "/bad?x=1", "/bad'path"])
def test_invalid_proxy_prefix_is_rejected(monkeypatch, prefix):
    monkeypatch.setenv("BC_NOVNC_PATH", prefix)
    with pytest.raises(ValueError):
        novnc_src_script()
