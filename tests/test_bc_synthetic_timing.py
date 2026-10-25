"""Synthetic timing keeps real DOM capture and restores runtime behavior."""

import time
from unittest import mock

import pytest
from playwright.sync_api import TimeoutError, sync_playwright

from billcollector import pagestate
from tests import run_regression


def test_synthetic_capture_and_timeout_restore_after_failure(monkeypatch):
    monkeypatch.setattr(pagestate, "DOM_SETTLE_MS", 3000)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            context = browser.new_context()
            initialize = mock.Mock(return_value=context)
            monkeypatch.setattr(run_regression.batch, "InitBrowser", initialize)
            with pytest.raises(RuntimeError, match="synthetic failure"):
                with run_regression.synthetic_browser_timing():
                    context = run_regression.batch.InitBrowser(playwright, None)
                    page = context.new_page()
                    page.set_content('<button id="save">Save</button>')
                    state = pagestate.PageState("synthetic", 1, page)
                    assert any(entry["name"] == "Save"
                               and entry["rect"]["w"] > 0
                               for entry in state.interactive_elements)
                    start = time.monotonic()
                    with pytest.raises(TimeoutError):
                        page.get_by_text("Absent synthetic control").click()
                    assert time.monotonic() - start < 10
                    raise RuntimeError("synthetic failure")

            assert pagestate.DOM_SETTLE_MS == 3000
            assert run_regression.batch.InitBrowser is initialize
            # Runtime settling still captures content injected after navigation.
            page.set_content('''<script>
                setTimeout(() => {
                    const button = document.createElement('button');
                    button.textContent = 'Deferred';
                    document.body.append(button);
                }, 100);
            </script>''')
            state = pagestate.PageState("synthetic", 2, page)
            assert any(entry["name"] == "Deferred"
                       for entry in state.interactive_elements)
        finally:
            browser.close()


def test_vault_mode_keeps_original_timing(monkeypatch):
    monkeypatch.setattr(pagestate, "DOM_SETTLE_MS", 3000)
    initialize = run_regression.batch.InitBrowser
    with run_regression.synthetic_browser_timing(enabled=False):
        assert pagestate.DOM_SETTLE_MS == 3000
        assert run_regression.batch.InitBrowser is initialize
