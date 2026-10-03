"""Running page scripts from asyncio.

`QWebEnginePage.runJavaScript` is callback-based and does not wait for
promises. These wrappers turn both into awaitables with a timeout, so the
automation code reads top to bottom.

Scripts run in the page's MAIN world: the ChatGPT composer only reacts to
events dispatched there.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineScript

MAIN_WORLD = QWebEngineScript.ScriptWorldId.MainWorld.value


class JsError(RuntimeError):
    """A page script threw, or the page went away."""


class JsTimeout(JsError):  # noqa: N818
    """A page script did not answer in time."""


async def run_js(page: QWebEnginePage, code: str, timeout: float = 15.0) -> Any:
    """Evaluate `code` in the page and return its (synchronous) result."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    def deliver(result: Any) -> None:
        if not future.done():
            loop.call_soon_threadsafe(lambda: future.done() or future.set_result(result))

    try:
        page.runJavaScript(code, MAIN_WORLD, deliver)
    except RuntimeError as error:  # the page object was deleted
        raise JsError(str(error)) from error

    try:
        return await asyncio.wait_for(future, timeout)
    except TimeoutError as error:
        raise JsTimeout("The page did not answer.") from error


async def run_js_json(page: QWebEnginePage, expression: str, timeout: float = 15.0) -> Any:
    """Evaluate an expression and bring its value back as JSON.

    Qt converts JS values to Python loosely (undefined and null both become
    empty, nested objects can be dropped). Round-tripping through JSON text is
    exact.
    """
    code = (
        "(() => { try { return JSON.stringify({ ok: true, value: (" + expression + ") }); }"
        " catch (error) { return JSON.stringify({ ok: false, error: String(error && error.message || error) }); } })()"
    )
    raw = await run_js(page, code, timeout)
    if not isinstance(raw, str) or not raw:
        raise JsError("The page returned nothing.")
    payload = json.loads(raw)
    if not payload.get("ok"):
        raise JsError(str(payload.get("error") or "script failed"))
    return payload.get("value")


async def call_async_js(page: QWebEnginePage, expression: str, timeout: float = 60.0, poll: float = 0.2) -> Any:
    """Evaluate an expression that returns a Promise and await its value.

    The result is parked on the page under a random key and polled for,
    because runJavaScript returns before a promise settles.
    """
    key = "k" + uuid.uuid4().hex
    start = (
        "(() => { window.__raiJobs = window.__raiJobs || {};"
        f" window.__raiJobs['{key}'] = {{ done: false }};"
        f" Promise.resolve().then(() => ({expression}))"
        f" .then((value) => {{ window.__raiJobs['{key}'] = {{ done: true, value }}; }},"
        f" (error) => {{ window.__raiJobs['{key}'] ="
        " { done: true, error: String(error && error.message || error) }; });"
        " return true; })()"
    )
    read = (
        f"(() => {{ const job = (window.__raiJobs || {{}})['{key}'];"
        f" if (job && job.done) delete window.__raiJobs['{key}'];"
        " return JSON.stringify(job === undefined ? null : job); })()"
    )

    await run_js(page, start, timeout=min(timeout, 15.0))

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        raw = await run_js(page, read, timeout=15.0)
        job = json.loads(raw) if isinstance(raw, str) and raw else None
        if job is None:
            # The page navigated away and took the parked result with it.
            raise JsError("The page changed while a script was running.")
        if job.get("done"):
            if "error" in job:
                raise JsError(str(job["error"]))
            return job.get("value")
        if loop.time() >= deadline:
            raise JsTimeout("The page script took too long.")
        await asyncio.sleep(poll)


async def ensure_script(page: QWebEnginePage, marker: str, source: str) -> None:
    """Inject `source` once per document. `marker` is a global the script defines."""
    present = await run_js(page, f"typeof {marker} !== 'undefined'")
    if present is True:
        return
    await run_js(page, source + "\n;true;")
    if await run_js(page, f"typeof {marker} !== 'undefined'") is not True:
        raise JsError(f"The page refused the script that defines {marker}.")


def js_string(value: str) -> str:
    """A Python string as a JavaScript string literal. Page data is never spliced in raw."""
    return json.dumps(value).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
