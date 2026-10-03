"""The embedded browser: new windows become tabs, and no site can raise a prompt."""

from __future__ import annotations

import asyncio

from PySide6.QtCore import QUrl

from app.browser.browser import BrowserWidget, host_matches
from app.browser.jsbridge import call_async_js, run_js
from app.browser.profile import CHROMIUM_FLAGS, apply_chromium_flags, chrome_user_agent
from tests.helpers import FIXTURES

PAGE = QUrl.fromLocalFile(str(FIXTURES / "browser" / "opener.html")).toString()


async def opened(browser: BrowserWidget):
    view = browser.add_tab(PAGE)
    assert await view.wait_loaded(20, settle=0.1)
    return view


async def test_a_target_blank_link_opens_a_tab_in_this_browser(qtbot, web_profile):
    browser = BrowserWidget(web_profile)
    qtbot.addWidget(browser)
    events: list[tuple[object, object]] = []
    browser.tab_opened.connect(lambda view, opener: events.append((view, opener)))

    first = await opened(browser)
    await run_js(first.page(), "document.getElementById('open').click()")
    for _ in range(50):
        if len(browser.views()) == 2:
            break
        await asyncio.sleep(0.1)

    assert len(browser.views()) == 2
    second = browser.views()[1]
    assert second.opener is first and events[-1] == (second, first)
    assert await second.wait_loaded(20, settle=0.1)
    assert second.current_url().endswith("target.html")
    assert browser.current() is second and browser.address.text().endswith("target.html")
    assert browser.address.isReadOnly()


async def test_permission_requests_are_denied_without_a_prompt(qtbot, web_profile):
    browser = BrowserWidget(web_profile)
    qtbot.addWidget(browser)
    view = await opened(browser)
    page = view.page()

    result = await call_async_js(
        page,
        "new Promise((resolve) => { try { Notification.requestPermission().then(resolve, () => resolve('error')); }"
        " catch (e) { resolve('error'); } setTimeout(() => resolve('timeout'), 4000); })",
        timeout=10,
    )
    assert result in ("denied", "default", "error")  # never 'granted', and never left hanging
    # The request reached the app and was refused there: no dialog was ever involved.
    assert page.denied_permissions >= 1


async def test_javascript_dialogs_cannot_freeze_a_page(qtbot, web_profile):
    browser = BrowserWidget(web_profile)
    qtbot.addWidget(browser)
    view = await opened(browser)
    value = await run_js(
        view.page(), "alert('x'); const c = confirm('y'); const p = prompt('z'); String(c) + ':' + String(p)"
    )
    # "Dismissed" means Cancel: the app never agrees to anything on a page's behalf.
    assert value == "false:null"
    assert view.page().dismissed_dialogs == 3


async def test_closing_tabs(qtbot, web_profile):
    browser = BrowserWidget(web_profile)
    qtbot.addWidget(browser)
    closed: list[object] = []
    browser.tab_closed.connect(closed.append)
    one = await opened(browser)
    two = await opened(browser)
    browser.close_tab(one)
    assert browser.views() == [two] and closed == [one]
    browser.close_all()
    assert browser.views() == [] and browser.address.text() == ""


def test_home_buttons_only_reach_allowed_hosts(qtbot, web_profile):
    browser = BrowserWidget(web_profile, allowed_hosts=["jobright.ai", "hiring.cafe", "hiringcafe.com"])
    qtbot.addWidget(browser)
    assert browser.allows("https://jobright.ai/jobs")
    assert browser.allows("https://www.hiring.cafe/")
    assert not browser.allows("https://example.com/")
    assert not browser.allows("https://jobright.ai.evil.example/")
    assert browser.open_home("https://example.com/") is None and browser.views() == []
    assert host_matches("app.jobright.ai", ["jobright.ai"]) and not host_matches("notjobright.ai", ["jobright.ai"])


def test_user_agent_has_no_qtwebengine_token(web_profile):
    agent = chrome_user_agent(web_profile)
    assert "QtWebEngine" not in agent and "Chrome/" in agent


def test_chromium_flags_keep_background_tabs_alive(monkeypatch):
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
    apply_chromium_flags()
    import os

    flags = os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]
    assert "--no-sandbox" in flags and all(flag in flags for flag in CHROMIUM_FLAGS)


async def test_pages_keep_running_while_hidden_when_asked(qtbot, web_profile):
    """A run goes on while another sidebar tab is in front: its pages must not be throttled."""
    probe = (
        "new Promise((resolve) => { const start = performance.now(); let n = 0;"
        " const tick = () => { n += 1; if (n >= 10) resolve(performance.now() - start); else setTimeout(tick, 50); };"
        " setTimeout(tick, 50); })"
    )

    kept = BrowserWidget(web_profile, keep_pages_active=True)
    qtbot.addWidget(kept)
    kept.show()
    view = kept.add_tab(PAGE)
    assert await view.wait_loaded(20, settle=0.1)
    kept.hide()
    await asyncio.sleep(0.3)
    assert view.page().isVisible()
    elapsed = await call_async_js(view.page(), probe, timeout=30)
    assert elapsed < 2500  # ten 50 ms timers; throttled, they would take about ten seconds

    plain = BrowserWidget(web_profile)
    qtbot.addWidget(plain)
    plain.show()
    other = plain.add_tab(PAGE)
    assert await other.wait_loaded(20, settle=0.1)
    plain.hide()
    await asyncio.sleep(0.3)
    assert not other.page().isVisible()


async def test_leaving_a_page_is_always_allowed(qtbot, web_profile):
    browser = BrowserWidget(web_profile)
    qtbot.addWidget(browser)
    view = await opened(browser)
    page = view.page()
    assert page.javaScriptConfirm(view.url(), "Are you sure you want to leave this page? Changes may not be saved.")
    assert not page.javaScriptConfirm(view.url(), "Submit your application now?")


async def test_wait_loaded_after_reload_waits_for_the_new_load(qtbot, web_profile):
    browser = BrowserWidget(web_profile)
    qtbot.addWidget(browser)
    view = await opened(browser)
    loads = view.loads
    view.reload()
    assert view.loading and view.load_ok is None  # marked at once, before loadStarted arrives
    assert await view.wait_loaded(20, settle=0)
    assert view.loads == loads + 1
