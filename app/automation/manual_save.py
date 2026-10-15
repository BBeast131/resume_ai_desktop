"""Job Search, manual mode: save the job page the user is looking at, when they ask.

In manual mode the user browses any job site and presses "Save job" on a
posting they have reviewed. The page is read the same way as in auto mode
(page text, page data, the page's own API answers) and the AI check confirms
role and company. Then:

  * everything found (role, company, a real description) -> saved at once;
  * something missing, or the AI check doubts the page -> the review dialog
    opens with what was found, and the user completes or corrects it;
  * a page that can never become a resume is refused with a toast: LinkedIn
    (the generator skips LinkedIn jobs), ChatGPT, a JobRight page that is not a
    job, or a page that needs sign-in / a human check and shows no description.

The app only READS the page. It never clicks Apply or submits anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from PySide6.QtWebEngineCore import QWebEnginePage

from app.automation.evidence import fuse, page_evidence
from app.automation.extractor import extract_page
from app.automation.job_rules import MIN_JD_CHARS, blocked_toast, is_jobright_detail_url, saved_toast, site_of, url_key
from app.data.store import Store
from app.services.jobcheck import check_job
from app.sync.api_client import WebApi

log = logging.getLogger(__name__)

TOAST_MANUAL_LINKEDIN = "Not saved: LinkedIn jobs can't be generated"
TOAST_MANUAL_NOT_A_JOB = "Not saved: open a job posting first"
TOAST_MANUAL_JOBRIGHT_LIST = "Not saved: open the job's own page on JobRight first"
TOAST_ALREADY_SAVED = "Already in Saved Jobs"
TOAST_ALREADY_GENERATED = "Already in Generated Resumes"

DOUBT_NOT_A_POSTING = "The AI check thinks this page may not be a single job posting. Check the details before saving."
DOUBT_NO_DESCRIPTION = "The AI check found no full job description on this page. Paste it below if you can see it."
MISSING_DETAILS = "Some details could not be read from this page. Fill them in, then save."


@dataclass(frozen=True)
class ManualDraft:
    """What was read from the page, ready to save or to show in the review dialog."""

    url: str
    source_site: str
    role: str = ""
    company: str = ""
    jd_text: str = ""
    confidence: float | None = None
    #: Why this page cannot be saved at all (a toast); None when it can.
    refused: str | None = None
    #: Why the user should look before saving (shown in the dialog); None when all is clear.
    doubt: str | None = None

    @property
    def complete(self) -> bool:
        return bool(self.role.strip() and self.company.strip() and len(self.jd_text.strip()) >= MIN_JD_CHARS)

    @property
    def ready(self) -> bool:
        """Can be saved without asking: nothing refused, nothing missing, nothing doubtful."""
        return self.refused is None and self.doubt is None and self.complete


@dataclass(frozen=True)
class ManualSaveResult:
    #: saved | duplicate_saved | duplicate_generated | invalid
    code: str
    toast: str
    #: success | info | warning
    kind: str
    job_id: str | None = None

    @property
    def saved(self) -> bool:
        return self.code == "saved"


def source_for(url: str) -> str:
    site = site_of(url)
    return site if site in ("jobright", "hiring_cafe") else "other"


async def read_manual(page: QWebEnginePage, api: WebApi) -> ManualDraft:
    """Read the job on `page` for a manual save."""
    extraction = await extract_page(page)
    url = extraction.url or page.url().toString()
    site = site_of(url)
    if not url.startswith(("http://", "https://")):
        return ManualDraft(url, "other", refused=TOAST_MANUAL_NOT_A_JOB)
    if extraction.is_linkedin or extraction.linkedin_source or site == "linkedin":
        return ManualDraft(url, source_for(url), refused=TOAST_MANUAL_LINKEDIN)
    if site == "chatgpt":
        return ManualDraft(url, "other", refused=TOAST_MANUAL_NOT_A_JOB)
    if site == "jobright" and not is_jobright_detail_url(url):
        return ManualDraft(url, "jobright", refused=TOAST_MANUAL_JOBRIGHT_LIST)
    if extraction.blocked and not extraction.has_jd:
        return ManualDraft(url, source_for(url), refused=blocked_toast(extraction.block_reason))

    # The same sources as the generator: page data and API answers beat a layout reading.
    fused = fuse(page_evidence(extraction))
    jd_text = fused.jd.value or (extraction.jd_text.strip() if extraction.has_jd else "")
    role = fused.role.value or extraction.role
    company = fused.company.value or extraction.company

    check = await check_job(api, extraction, jd_text or extraction.jd_text)
    doubt: str | None = None
    if not check.unchecked:
        role = check.role or role
        company = check.company or company
        if not check.is_job_posting:
            doubt = DOUBT_NOT_A_POSTING
        elif not check.has_job_description and len(jd_text) < MIN_JD_CHARS:
            doubt = DOUBT_NO_DESCRIPTION
    draft = ManualDraft(
        url=url,
        source_site=source_for(url),
        role=role.strip()[:160],
        company=company.strip()[:160],
        jd_text=jd_text,
        confidence=check.confidence,
        doubt=doubt,
    )
    if doubt is None and not draft.complete:
        draft = replace(draft, doubt=MISSING_DETAILS)
    log.info(
        "manual.read site=%s ready=%s role=%s company=%s jd_length=%d",
        draft.source_site,
        draft.ready,
        bool(draft.role),
        bool(draft.company),
        len(draft.jd_text),
    )
    return draft


def save_manual(
    store: Store,
    *,
    url: str,
    role: str,
    company: str,
    jd_text: str,
    confidence: float | None = None,
) -> ManualSaveResult:
    """Add the job to Saved Jobs, exactly like an auto-mode save (it syncs and generates the same way)."""
    role, company, jd_text = role.strip(), company.strip(), jd_text.strip()
    if not url.startswith(("http://", "https://")) or not role or not company or len(jd_text) < MIN_JD_CHARS:
        return ManualSaveResult("invalid", "Not saved: role, company and the job description are needed", "warning")
    result = store.add_saved_job(
        url=url[:2048],
        url_key=url_key(url),
        source_site=source_for(url),
        role=role[:160],
        company=company[:160],
        jd_text=jd_text,
        confidence=confidence,
    )
    log.info("manual.save outcome=%s source=%s jd_length=%d", result.outcome, source_for(url), len(jd_text))
    if result.outcome == "duplicate_generated":
        return ManualSaveResult("duplicate_generated", TOAST_ALREADY_GENERATED, "info")
    if not result.saved:
        return ManualSaveResult("duplicate_saved", TOAST_ALREADY_SAVED, "info")
    return ManualSaveResult("saved", saved_toast(role, company), "success", result.job.id if result.job else None)
