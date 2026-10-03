"""Shared test setup.

Qt runs offscreen, and asyncio runs on the Qt event loop (qasync), so a test
can simply `await` code that drives a QWebEnginePage.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
if "--no-sandbox" not in _flags and hasattr(os, "geteuid") and os.geteuid() == 0:
    # Chromium refuses to run as root with its sandbox on (CI containers).
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (_flags + " --no-sandbox").strip()

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
import qasync  # noqa: E402
from PySide6.QtCore import QEventLoop  # noqa: E402
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="session")
def qapp() -> Iterator[QApplication]:
    app = QApplication.instance() or QApplication(["resume-ai-tests"])
    assert isinstance(app, QApplication)
    yield app


_LOOP: qasync.QEventLoop | None = None


def qt_loop() -> qasync.QEventLoop:
    """The one asyncio loop of the test run: the Qt event loop itself.

    It is created as "already running" and never stopped. Stopping a qasync
    loop calls QApplication.exit(), which makes QtWebEngine shut its profiles
    down; doing that once per test crashes Chromium on the second test.
    """
    global _LOOP
    if _LOOP is None:
        app = QApplication.instance() or QApplication(["resume-ai-tests"])
        _LOOP = qasync.QEventLoop(app, set_running_loop=True, already_running=True)
        asyncio.set_event_loop(_LOOP)
    return _LOOP


def run_on_qt_loop(coroutine: Coroutine[Any, Any, Any], timeout: float = 180.0) -> Any:
    """Run a coroutine to completion by spinning a local Qt event loop."""
    loop = qt_loop()
    task = loop.create_task(asyncio.wait_for(coroutine, timeout))
    spinner = QEventLoop()
    task.add_done_callback(lambda _task: spinner.quit())
    if not task.done():
        spinner.exec()
    return task.result()


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    """Run `async def` tests on the Qt loop. No plugin, no per-test loops."""
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    names = pyfuncitem._fixtureinfo.argnames
    kwargs = {name: pyfuncitem.funcargs[name] for name in names}
    run_on_qt_loop(pyfuncitem.obj(**kwargs))
    return True


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets its own app-data folder and never touches the real one."""
    monkeypatch.setenv("RESUME_AI_DATA_DIR", str(tmp_path / "appdata"))


@pytest.fixture(scope="session")
def web_profile(qapp: QApplication) -> Iterator[QWebEngineProfile]:
    # Off the record: nothing is written to disk. One for the whole run, and
    # owned by the application, because a profile must outlive its pages.
    profile = QWebEngineProfile(qapp)
    yield profile
    # Chromium must be torn down before the interpreter starts collecting Qt
    # objects in arbitrary order, or the process dies on the way out.
    import shiboken6

    for page in profile.findChildren(QWebEnginePage):
        shiboken6.delete(page)
    shiboken6.delete(profile)
    qapp.processEvents()


@pytest.fixture
def web_page(web_profile: QWebEngineProfile) -> Iterator[QWebEnginePage]:
    page = QWebEnginePage(web_profile, web_profile)
    yield page
    page.deleteLater()
