"""Driving the ChatGPT tab: paste the prompt, send it, read the reply.

The page side lives in `js/bridge.js` (a port of the extension's
`chatgpt-bridge.ts`; every behaviour there was measured on the live site).
This module is the asyncio side: it injects the bridge, starts the paste,
shows its progress, and polls for the reply with the extension's completion
rules.

Nothing here logs or prints the prompt or the reply. Lengths and counts only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Callable
from functools import cache
from typing import Any

from PySide6.QtWebEngineCore import QWebEnginePage

from app.automation.chat_url import normalize_chat_url
from app.browser.jsbridge import JsError, call_async_js, ensure_script, js_string, run_js, run_js_json
from app.config import resource_path

log = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]

BRIDGE_MARKER = "window.__RAI_BRIDGE__"

#: How often the reply is read while waiting.
POLL_SECONDS = 1.2
#: Not answering, and the text has held still this long: done.
STABLE_SECONDS = 1.8
#: One complete JSON object unchanged this long: done, whatever the buttons say.
COMPLETE_JSON_QUIET_SECONDS = 3.0
#: A "log in" control must stay on screen this long before the run is paused (it can flash while the app loads).
LOGIN_GRACE_SECONDS = 1.0
#: Cloudflare's automatic check clears itself within a few seconds; only a lasting one needs a person.
CHALLENGE_GRACE_SECONDS = 6.0
#: The bridge splits the prompt into chunks of at most this many characters.
PASTE_CHUNK_LIMIT = 8000

_BLOCKED_MESSAGES = {
    "login": "ChatGPT is asking you to log in. Log in on the ChatGPT tab, then continue.",
    "challenge": 'ChatGPT is showing a "verify you are human" check. Complete it on the ChatGPT tab, then continue.',
}

_SUBMIT_MESSAGES = {
    "composer-not-found": "Could not find the ChatGPT message box. The page layout may have changed.",
    "send-failed": "The prompt was pasted into ChatGPT but it would not send.",
}


class ChatGPTError(Exception):
    """The ChatGPT tab could not do what was asked. The message is safe to show."""


class ChatGPTBlocked(ChatGPTError):  # noqa: N818 - the name the rest of the app uses
    """The page needs a real person: `reason` is 'login' or 'challenge'."""

    def __init__(self, reason: str) -> None:
        super().__init__(_BLOCKED_MESSAGES.get(reason, "ChatGPT needs your attention before the run can continue."))
        self.reason = reason


class ReplyTimeout(ChatGPTError):  # noqa: N818 - the name the rest of the app uses
    """ChatGPT did not finish answering in time."""


class Cancelled(ChatGPTError):  # noqa: N818 - the name the rest of the app uses
    """Stop was pressed."""

    def __init__(self) -> None:
        super().__init__("Stopped.")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

# JavaScript's `\s`, so the port answers exactly as the page-side function does.
_JS_SPACE_OR_FENCE = re.compile(
    "[\t\n\x0b\x0c\r \xa0  -     　﻿`]*",
)


def looks_like_complete_json(text: str) -> bool:
    """Is `text` one finished JSON object or array, with nothing after it but space and fences?

    A port of `looksLikeCompleteJson`. It only counts brackets outside strings;
    it does not parse. A reply that passes and has stopped changing is done,
    even when the page still shows a Stop button.
    """
    start = text.find("{")
    if start < 0:
        return False
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth < 0:
                return False
            if depth == 0:
                return _JS_SPACE_OR_FENCE.fullmatch(text, index + 1) is not None
    return False


def contains_json_object(text: str) -> bool:
    """The cheap local pre-check: does the reply contain one JSON object?

    Deliberately lenient. The web app repairs nearly-valid JSON (trailing
    commas, smart quotes, a sentence before the object), so anything that has
    an opening brace, a later closing brace and at least one key between them
    is sent on for the real validation. Only a reply with no object at all
    (prose, an apology, an empty message) is turned away here.
    """
    first = text.find("{")
    last = text.rfind("}")
    if first < 0 or last <= first:
        return False
    span = text[first : last + 1]
    try:
        parsed = json.loads(span)
    except (ValueError, RecursionError):
        return ":" in span
    return isinstance(parsed, dict) and len(parsed) > 0


def conversation_url(page: QWebEnginePage) -> str | None:
    """The tab's conversation link, `https://chatgpt.com/c/<id>`, once it has one."""
    try:
        return normalize_chat_url(page.url().toString())
    except RuntimeError:  # the page object was deleted
        return None


def waiting_text(blocks: int, chars: int, stop_shown: bool) -> str:
    """The status line while waiting: "Waiting for ChatGPT (1 reply block, 8,412 chars, Stop shown)"."""
    noun = "reply block" if blocks == 1 else "reply blocks"
    return f"Waiting for ChatGPT ({blocks} {noun}, {chars:,} chars, Stop {'shown' if stop_shown else 'gone'})"


def pasting_text(chunk: int, chunks: int) -> str:
    """The status line while pasting: "Pasting prompt 3/14…"."""
    return f"Pasting prompt {chunk}/{chunks}…"


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


@cache
def _bridge_source() -> str:
    return resource_path("app", "automation", "js", "bridge.js").read_text(encoding="utf-8")


async def ensure_bridge(page: QWebEnginePage) -> None:
    """Inject `bridge.js` unless this document already has it."""
    await ensure_script(page, BRIDGE_MARKER, _bridge_source())


def _check_cancel(cancel: asyncio.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


async def _pause(seconds: float, cancel: asyncio.Event | None) -> None:
    """Sleep, but wake up at once (raising Cancelled) when Stop is pressed."""
    if cancel is None:
        await asyncio.sleep(seconds)
        return
    _check_cancel(cancel)
    try:
        await asyncio.wait_for(cancel.wait(), seconds)
    except TimeoutError:
        return
    raise Cancelled()


class _BlockWatch:
    """Reports a blocked page only once it has stayed blocked for its grace period."""

    def __init__(self) -> None:
        self.reason: str | None = None
        self.since = 0.0

    def observe(self, reason: str | None, now: float) -> str | None:
        if reason not in _BLOCKED_MESSAGES:
            self.reason = None
            return None
        if reason != self.reason:
            self.reason = reason
            self.since = now
        grace = LOGIN_GRACE_SECONDS if reason == "login" else CHALLENGE_GRACE_SECONDS
        return reason if now - self.since >= grace else None


def _emit(on_progress: ProgressFn | None, text: str, last: list[str]) -> None:
    """Report a status line, skipping an exact repeat of the previous one."""
    if on_progress is None or (last and last[0] == text):
        return
    last[:] = [text]
    on_progress(text)


def _submit_progress_text(progress: Any) -> str | None:
    if not isinstance(progress, dict):
        return None
    phase = progress.get("phase")
    if phase == "pasting":
        chunk, chunks = progress.get("chunk"), progress.get("chunks")
        if isinstance(chunk, int) and isinstance(chunks, int):
            return pasting_text(chunk, chunks)
        return None
    return {
        "composer": "Waiting for the ChatGPT message box…",
        "clearing": "Clearing the old draft…",
        "sending": "Sending prompt…",
        "sent": "Prompt sent",
    }.get(str(phase))


async def _read_submit_progress(page: QWebEnginePage) -> Any:
    try:
        return await run_js_json(page, "window.__raiBridgeProgress || null", timeout=5.0)
    except JsError:
        return None


async def _submit_once(
    page: QWebEnginePage,
    prompt: str,
    on_progress: ProgressFn | None,
    cancel: asyncio.Event | None,
    last: list[str],
) -> dict[str, Any]:
    """Run the page-side submit once, relaying its progress and the Stop request."""
    chunks = len(prompt) // PASTE_CHUNK_LIMIT + 1
    # Worst case inside the page: 15 s for the composer, four tries per chunk, 25 s to send.
    timeout = 75.0 + 1.6 * chunks
    expression = (
        "(window.__raiBridgeCancel = false, window.__raiBridgeProgress = null, "
        f"{BRIDGE_MARKER}.submitPrompt({js_string(prompt)}))"
    )
    task = asyncio.ensure_future(call_async_js(page, expression, timeout=timeout, poll=0.1))
    stop_sent = False
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=0.1)
            if done:
                break
            if cancel is not None and cancel.is_set() and not stop_sent:
                # The page script stops at its next safe point: between chunks, or before sending.
                with contextlib.suppress(JsError):
                    await run_js(page, "window.__raiBridgeCancel = true; true", timeout=5.0)
                    stop_sent = True
            text = _submit_progress_text(await _read_submit_progress(page))
            if text:
                _emit(on_progress, text, last)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, JsError):
                await task

    try:
        result = task.result()
    except JsError as error:
        if cancel is not None and cancel.is_set():
            raise Cancelled() from error
        reason = await detect_blocked(page)
        if reason:
            raise ChatGPTBlocked(reason) from error
        raise ChatGPTError("The ChatGPT page stopped answering while the prompt was being pasted.") from error

    # The paste is quick; make sure its last step was shown even if no poll caught it.
    text = _submit_progress_text(await _read_submit_progress(page))
    if text:
        _emit(on_progress, text, last)
    return result if isinstance(result, dict) else {"status": "error"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def detect_blocked(page: QWebEnginePage) -> str | None:
    """'login', 'challenge' or None, for the page as it is right now."""
    try:
        await ensure_bridge(page)
        reason = await run_js_json(page, f"{BRIDGE_MARKER}.detectBlocked()")
    except JsError:
        return None
    return reason if reason in _BLOCKED_MESSAGES else None


async def wait_for_composer(page: QWebEnginePage, timeout: float = 45.0, cancel: asyncio.Event | None = None) -> None:
    """Wait until ChatGPT's message box is on screen and nothing stands in the way.

    Raises ChatGPTBlocked when the page shows "log in" or a challenge that
    does not clear by itself, ChatGPTError when no message box appears in
    time, and Cancelled when Stop is pressed.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    watch = _BlockWatch()
    seen: str | None = None

    while True:
        _check_cancel(cancel)
        state: Any = None
        try:
            await ensure_bridge(page)
            state = await run_js_json(
                page,
                f"({{ ready: {BRIDGE_MARKER}.composerReady(), blocked: {BRIDGE_MARKER}.detectBlocked() }})",
                timeout=5.0,
            )
        except JsError:
            state = None  # still loading, or between two documents

        now = loop.time()
        if isinstance(state, dict):
            seen = state.get("blocked") if state.get("blocked") in _BLOCKED_MESSAGES else None
            reason = watch.observe(seen, now)
            if reason:
                raise ChatGPTBlocked(reason)
            if seen is None and state.get("ready") is True:
                return

        if now >= deadline:
            if seen:
                raise ChatGPTBlocked(seen)
            raise ChatGPTError("ChatGPT's message box did not appear. Check the ChatGPT tab.")
        await _pause(0.4, cancel)


async def count_replies(page: QWebEnginePage) -> int:
    """How many non-empty assistant replies the conversation holds right now."""
    await ensure_bridge(page)
    try:
        value = await run_js_json(page, f"{BRIDGE_MARKER}.countReplies()")
    except JsError as error:
        raise ChatGPTError("The ChatGPT page did not answer.") from error
    return value if isinstance(value, int) and value >= 0 else 0


async def submit_prompt(
    page: QWebEnginePage,
    prompt: str,
    on_progress: ProgressFn | None = None,
    cancel: asyncio.Event | None = None,
) -> int:
    """Paste `prompt` into the composer in chunks and send it.

    Returns the reply-count baseline taken just before sending: pass it to
    `wait_for_reply`, which then accepts text only from a reply beyond it. In
    a fresh chat that is 0; for a follow-up it keeps the earlier reply from
    being read back as the answer.

    Raises ChatGPTBlocked, ChatGPTError, or Cancelled (in which case the
    prompt was not sent unless Stop arrived after the send itself).
    """
    _check_cancel(cancel)
    if not prompt.strip():
        raise ChatGPTError("There is no prompt to send.")

    try:
        await ensure_bridge(page)
    except JsError as error:
        raise ChatGPTError("The ChatGPT page did not accept the automation script.") from error
    baseline = await count_replies(page)

    last: list[str] = []
    result = await _submit_once(page, prompt, on_progress, cancel, last)

    if result.get("status") == "composer-not-found" and not (cancel is not None and cancel.is_set()):
        # The reference retries once: the app can still be mounting its composer.
        if (reason := await detect_blocked(page)) is not None:
            raise ChatGPTBlocked(reason)
        log.info("ChatGPT submit: composer not found, retrying once")
        await _pause(1.2, cancel)
        await ensure_bridge(page)
        result = await _submit_once(page, prompt, on_progress, cancel, last)

    status = str(result.get("status"))
    # The diagnostic holds step names, lengths and counts; never prompt text.
    log.info("ChatGPT submit: status=%s chars=%d detail=%s", status, len(prompt), result.get("diagnostic") or "-")

    if status == "cancelled" or (cancel is not None and cancel.is_set()):
        raise Cancelled()
    if status == "not-signed-in":
        raise ChatGPTBlocked("login")
    if status != "ok":
        if (reason := await detect_blocked(page)) is not None:
            raise ChatGPTBlocked(reason)
        raise ChatGPTError(_SUBMIT_MESSAGES.get(status, "The prompt could not be sent to ChatGPT."))
    return baseline


async def wait_for_reply(
    page: QWebEnginePage,
    baseline: int,
    timeout: float,
    on_progress: ProgressFn | None = None,
    cancel: asyncio.Event | None = None,
) -> str:
    """Wait for the reply beyond `baseline` to finish, and return its text.

    Done when ChatGPT is not answering and the text has held still for 1.8 s,
    or when the text is one complete JSON object unchanged for 3 s.

    Raises ReplyTimeout after `timeout` seconds, Cancelled when Stop is
    pressed, and ChatGPTBlocked when the page turns into a log-in or challenge
    page while waiting.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    watch = _BlockWatch()
    last: list[str] = []

    last_text = ""
    unchanged_since = loop.time()
    last_length = 0
    stable_since: float | None = None

    expression = f"({{ poll: {BRIDGE_MARKER}.readBeyond({int(baseline)}), blocked: {BRIDGE_MARKER}.detectBlocked() }})"

    while True:
        _check_cancel(cancel)
        if loop.time() >= deadline:
            raise ReplyTimeout("ChatGPT did not finish responding in time. Check the ChatGPT tab.")

        await _pause(min(POLL_SECONDS, max(0.05, deadline - loop.time())), cancel)

        try:
            state = await run_js_json(page, expression)
        except JsError:
            # A transient read failure mid-stream is not fatal. If the document
            # was replaced, the bridge has to go back in before the next read.
            with contextlib.suppress(JsError):
                await ensure_bridge(page)
            continue
        if not isinstance(state, dict):
            continue

        now = loop.time()
        reason = watch.observe(state.get("blocked"), now)
        if reason:
            raise ChatGPTBlocked(reason)

        poll = state.get("poll")
        if not isinstance(poll, dict) or poll.get("status") != "ok":
            continue

        text = poll.get("text")
        if not isinstance(text, str):
            text = ""
        blocks = poll.get("blocks")
        _emit(
            on_progress,
            waiting_text(blocks if isinstance(blocks, int) else 0, len(text), bool(poll.get("stopVisible"))),
            last,
        )

        # A finished JSON reply that has stopped changing is done, whatever
        # the page's buttons say.
        if text != last_text:
            last_text = text
            unchanged_since = now
        elif text and now - unchanged_since >= COMPLETE_JSON_QUIET_SECONDS and looks_like_complete_json(text):
            log.info("ChatGPT reply: complete JSON, %d chars", len(text))
            return text

        if poll.get("streaming"):
            stable_since = None
            last_length = len(text)
            continue

        # Not streaming. The text must hold steady briefly before it is
        # accepted: the Stop button flickers away in the middle of a reply.
        if text and len(text) == last_length:
            if stable_since is None:
                stable_since = now
            if now - stable_since >= STABLE_SECONDS:
                log.info("ChatGPT reply: stable, %d chars", len(text))
                return text
        else:
            stable_since = None
            last_length = len(text)
