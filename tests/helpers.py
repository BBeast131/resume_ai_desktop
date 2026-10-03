"""Helpers for tests that drive a QWebEnginePage."""

from __future__ import annotations

import asyncio
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import QWebEnginePage

FIXTURES = Path(__file__).resolve().parent / "fixtures"


async def load_url(page: QWebEnginePage, url: QUrl, timeout: float = 30.0) -> bool:
    loop = asyncio.get_running_loop()
    done: asyncio.Future[bool] = loop.create_future()

    def finished(ok: bool) -> None:
        if not done.done():
            done.set_result(ok)

    page.loadFinished.connect(finished)
    try:
        page.load(url)
        return await asyncio.wait_for(done, timeout)
    finally:
        page.loadFinished.disconnect(finished)


async def load_fixture(page: QWebEnginePage, relative: str, timeout: float = 30.0) -> bool:
    """Load `tests/fixtures/<relative>` into the page and wait for it."""
    return await load_url(page, QUrl.fromLocalFile(str(FIXTURES / relative)), timeout)


async def load_html(
    page: QWebEnginePage, html: str, base_url: str = "https://example.test/", timeout: float = 30.0
) -> bool:
    """Load an HTML string as if it had been served from `base_url`."""
    loop = asyncio.get_running_loop()
    done: asyncio.Future[bool] = loop.create_future()

    def finished(ok: bool) -> None:
        if not done.done():
            done.set_result(ok)

    page.loadFinished.connect(finished)
    try:
        page.setHtml(html, QUrl(base_url))
        return await asyncio.wait_for(done, timeout)
    finally:
        page.loadFinished.disconnect(finished)
