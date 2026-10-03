"""Job Search capture: a job tab finished loading — save it, or say why not.

The rules (section 6.2 of the spec):
  * a jobright.ai job detail page is saved, with jobright's own description;
  * anything on linkedin.com, or a jobright job whose source is LinkedIn, is not;
  * a real job site reached from hiring.cafe is saved only if it has a real
    job description; a bare application form is not;
  * a page that needs a person (sign-in, consent wall, "verify you are human")
    is not saved, and the toast says why.

The app only READS the page. It never clicks anything during capture.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtWebEngineCore import QWebEnginePage

from app.automation.extractor import extract_page
from app.automation.job_rules import (
    MIN_JD_CHARS,
    TOAST_DUPLICATE,
    TOAST_NO_JD,
    decide_capture,
    saved_toast,
    site_of,
    url_key,
)
from app.data.store import Store
from app.services.jobcheck import check_job
from app.sync.api_client import WebApi

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CaptureResult:
    #: saved | duplicate | linkedin | form_only_no_jd | needs_user_action | not_a_job
    code: str
    toast: str
    #: success | info | warning
    kind: str
    job_id: str | None = None

    @property
    def saved(self) -> bool:
        return self.code == "saved"


async def capture_job(page: QWebEnginePage, opener_url: str, store: Store, api: WebApi) -> CaptureResult:
    """Read the page in `page` and apply the save rules. `opener_url` is the page the job was clicked on."""
    opener_site = site_of(opener_url)
    extraction = await extract_page(page)
    decision = decide_capture(extraction, opener_site)
    own_site = site_of(extraction.url)
    # The heuristic said "a bare form", but there is some text: the AI check gets the last word.
    doubtful_form = (
        not decision.save
        and decision.code == "form_only_no_jd"
        and extraction.form_only
        and len(extraction.jd_text.strip()) >= MIN_JD_CHARS
    )
    if not decision.save and not doubtful_form:
        kind = "warning" if decision.code == "needs_user_action" else "info"
        return CaptureResult(decision.code, decision.toast, kind)

    # Confirm or correct role and company. Falls back to the page's own values when unavailable.
    check = await check_job(api, extraction, decision.jd_text)
    confirmed = not check.unchecked and check.is_job_posting and check.has_job_description
    if doubtful_form and not confirmed:
        return CaptureResult(decision.code, decision.toast, "info")
    if own_site != "jobright" and not check.unchecked and not confirmed:
        # The model overrules the heuristics: this is a form, a list or an error page.
        return CaptureResult("form_only_no_jd", TOAST_NO_JD, "info")

    role = check.role or extraction.role
    company = check.company or extraction.company
    source = own_site if own_site in ("jobright", "hiring_cafe") else opener_site
    if source not in ("jobright", "hiring_cafe"):
        source = "other"

    result = store.add_saved_job(
        url=extraction.url[:2048],
        url_key=url_key(extraction.url),
        source_site=source,
        role=role[:160] or None,
        company=company[:160] or None,
        jd_text=decision.jd_text,
        confidence=check.confidence,
    )
    log.info("capture.done outcome=%s source=%s jd_length=%d", result.outcome, source, len(extraction.jd_text))
    if not result.saved:
        return CaptureResult("duplicate", TOAST_DUPLICATE, "info")
    return CaptureResult("saved", saved_toast(role, company), "success", result.job.id if result.job else None)
