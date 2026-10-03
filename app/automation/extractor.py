"""Reading a job page: the Python side of `js/extractor.js`.

The script is injected into whatever page the embedded browser has loaded and
asked two things: what the job is (`extract`) and whether a person has to do
something before it can be read (`classify`). Nothing here clicks through a
consent, login or security gate; the only clicks are a "Show more" toggle next
to the description and, on jobright, the Apply button the pipeline asks for.

Page text is never logged.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from PySide6.QtWebEngineCore import QWebEnginePage

from app.automation import job_rules
from app.browser.jsbridge import JsError, call_async_js, ensure_script, run_js_json
from app.config import resource_path

MARKER = "window.__RAI_EXTRACT__"
MAX_JD_CHARS = 50_000
#: How long a single-page site gets to finish rendering before the second read.
SETTLE_SECONDS = 1.2
#: How long a "Show more" click gets to reveal the text.
EXPAND_SECONDS = 0.4

_READ_TIMEOUT = 20.0
_SCROLL_TIMEOUT = 20.0
_MAX_FIELD_CHARS = 160
_MAX_TITLE_CHARS = 300
_MAX_URL_CHARS = 4_000
_MAX_APPLY_LINKS = 20


@dataclass
class Extraction:
    """What the extractor read from one page, and whether the page is blocked."""

    url: str = ""
    title: str = ""
    role: str = ""
    company: str = ""
    jd_text: str = ""
    jd_html_len: int = 0
    form_field_count: int = 0
    has_jobposting_ldjson: bool = False
    apply_links: list[str] = field(default_factory=list)
    is_linkedin: bool = False
    linkedin_source: bool = False
    prose_len: int = 0
    source: str = "generic"
    blocked: bool = False
    block_reason: str | None = None
    block_detail: str = ""

    @property
    def form_only(self) -> bool:
        """An application form with no job description around it."""
        return job_rules.is_form_only(self.has_jobposting_ldjson, self.prose_len, self.form_field_count)

    @property
    def has_jd(self) -> bool:
        """A real description is present: long enough, and not just a form."""
        return len(self.jd_text.strip()) >= job_rules.MIN_JD_CHARS and not self.form_only


@lru_cache(maxsize=1)
def _script() -> str:
    return resource_path("app", "automation", "js", "extractor.js").read_text(encoding="utf-8")


def tidy_jd(text: str) -> str:
    """Normalise line ends, collapse runs of blank lines, cap the length."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t ]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_JD_CHARS].rstrip()


# The script runs in the page's own world, so the page could tamper with what
# comes back. Every value is coerced to the type and size the app expects.
def _text(value: Any, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(0, min(int(value), 10_000_000))


def _links(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    links: list[str] = []
    for item in value:
        if isinstance(item, str) and item.lower().startswith(("http://", "https://")) and len(item) <= _MAX_URL_CHARS:
            if item not in links:
                links.append(item)
        if len(links) >= _MAX_APPLY_LINKS:
            break
    return links


def _page_url(page: QWebEnginePage) -> str:
    try:
        return str(page.url().toString())[:_MAX_URL_CHARS]
    except RuntimeError:  # the page object was deleted
        return ""


def _timed_out(page: QWebEnginePage, detail: str) -> Extraction:
    return Extraction(url=_page_url(page), blocked=True, block_reason="timeout", block_detail=detail)


def _from_payload(page: QWebEnginePage, payload: Any) -> Extraction:
    if not isinstance(payload, dict) or not isinstance(payload.get("extraction"), dict):
        return _timed_out(page, "the page did not answer the extractor")
    data: dict[str, Any] = payload["extraction"]
    verdict = payload.get("classification")
    verdict = verdict if isinstance(verdict, dict) else {}

    url = data.get("url")
    source = _text(data.get("source"), 60)
    reason = verdict.get("reason")
    blocked = bool(verdict.get("blocked"))
    if blocked and (not isinstance(reason, str) or reason not in job_rules.BLOCK_MESSAGES):
        reason = "empty"

    return Extraction(
        url=url[:_MAX_URL_CHARS] if isinstance(url, str) and url else _page_url(page),
        title=_text(data.get("title"), _MAX_TITLE_CHARS),
        role=_text(data.get("role"), _MAX_FIELD_CHARS),
        company=_text(data.get("company"), _MAX_FIELD_CHARS),
        jd_text=tidy_jd(data["jd_text"]) if isinstance(data.get("jd_text"), str) else "",
        jd_html_len=_count(data.get("jd_html_len")),
        form_field_count=_count(data.get("form_field_count")),
        has_jobposting_ldjson=data.get("has_jobposting_ldjson") is True,
        apply_links=_links(data.get("apply_links")),
        is_linkedin=data.get("is_linkedin") is True
        or job_rules.site_of(url if isinstance(url, str) else "") == "linkedin",
        linkedin_source=data.get("linkedin_source") is True,
        prose_len=_count(data.get("prose_len")),
        source=source if source in ("ldjson", "generic") or source.startswith("adapter:") else "generic",
        blocked=blocked,
        block_reason=reason if blocked else None,
        block_detail=_text(verdict.get("detail"), 200) if blocked else "",
    )


async def _read(page: QWebEnginePage) -> Extraction:
    try:
        await ensure_script(page, MARKER, _script())
        payload = await run_js_json(page, f"{MARKER}.read()", timeout=_READ_TIMEOUT)
    except JsError:
        # The page navigated away, was closed, or never answered.
        return _timed_out(page, "the page did not answer the extractor")
    return _from_payload(page, payload)


def _readable(result: Extraction) -> bool:
    return not result.blocked and result.has_jd


async def extract_page(page: QWebEnginePage, *, settle: bool = True) -> Extraction:
    """Read the job on `page` and say whether the page is blocked.

    With `settle`, a page that is blocked or has no description gets the
    harmless fixes, in order, with a fresh read after each: wait and re-read
    once, scroll to trigger lazy loading, expand "Show more" toggles next to
    the description. It stops trying as soon as the page reads fine. A
    readable page still gets its "Show more" toggle expanded, so a truncated
    description is not mistaken for the whole one.

    Never raises for a page problem: a page that went away or did not answer
    comes back as `blocked` with reason `timeout`.
    """
    result = await _read(page)
    if not settle:
        return result

    if not _readable(result):
        await asyncio.sleep(SETTLE_SECONDS)
        result = await _read(page)

    if not _readable(result):
        try:
            await ensure_script(page, MARKER, _script())
            await call_async_js(page, f"{MARKER}.scrollForLazy()", timeout=_SCROLL_TIMEOUT)
        except JsError:
            pass
        result = await _read(page)

    try:
        await ensure_script(page, MARKER, _script())
        clicked = await run_js_json(page, f"{MARKER}.expandDescription()")
    except JsError:
        clicked = 0
    if isinstance(clicked, int | float) and clicked > 0:
        await asyncio.sleep(EXPAND_SECONDS)
        result = await _read(page)

    return result


async def find_apply_control(page: QWebEnginePage) -> dict[str, Any] | None:
    """The "Apply with Autofill" / "Apply Now" control of a jobright detail page.

    Returns `{"text", "href", "tag"}` (href is None for a button), or None.
    """
    try:
        await ensure_script(page, MARKER, _script())
        found = await run_js_json(page, f"{MARKER}.findApplyControl()")
    except JsError:
        return None
    if not isinstance(found, dict):
        return None
    href = found.get("href")
    return {
        "text": _text(found.get("text"), 80),
        "href": href if isinstance(href, str) and href.lower().startswith(("http://", "https://")) else None,
        "tag": _text(found.get("tag"), 20),
    }


async def click_apply_control(page: QWebEnginePage) -> bool:
    """Click that control. True when something was clicked."""
    try:
        await ensure_script(page, MARKER, _script())
        return await run_js_json(page, f"{MARKER}.clickApplyControl()") is True
    except JsError:
        return False
