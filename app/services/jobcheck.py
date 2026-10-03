"""The Groq check on an extracted job page, done by the web app.

The web app holds the Groq key; the desktop only asks it to confirm or
correct the role and company. When the web app or Groq is unavailable the
page's own values stand and `confidence` is None, so capture keeps working
and the review step shows a warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.sync.api_client import ApiError, WebApi

#: Below this the review panel says "low confidence".
LOW_CONFIDENCE = 0.6


class ExtractionFacts(Protocol):
    url: str
    title: str
    role: str
    company: str
    jd_text: str
    form_field_count: int
    has_jobposting_ldjson: bool


@dataclass(frozen=True)
class JobCheck:
    role: str
    company: str
    is_job_posting: bool
    has_job_description: bool
    confidence: float | None
    notes: str
    role_corrected: bool
    company_corrected: bool
    #: `groq`, or `fallback` when the model was not consulted.
    source: str

    @property
    def low_confidence(self) -> bool:
        return self.confidence is not None and self.confidence < LOW_CONFIDENCE

    @property
    def unchecked(self) -> bool:
        return self.confidence is None


def unchecked(extraction: ExtractionFacts, note: str = "") -> JobCheck:
    return JobCheck(
        role=extraction.role.strip()[:160],
        company=extraction.company.strip()[:160],
        is_job_posting=True,
        has_job_description=len(extraction.jd_text.strip()) >= 100,
        confidence=None,
        notes=note,
        role_corrected=False,
        company_corrected=False,
        source="fallback",
    )


async def check_job(api: WebApi, extraction: ExtractionFacts, jd_text: str | None = None) -> JobCheck:
    text = jd_text if jd_text is not None else extraction.jd_text
    payload: dict[str, Any] = {
        "url": extraction.url[:2048],
        "title": extraction.title[:500],
        "role": extraction.role[:400],
        "company": extraction.company[:400],
        # Only the first part is needed for the check; the full text stays here.
        "jdText": text[:4000],
        "formFieldCount": max(0, min(10_000, int(extraction.form_field_count))),
        "hasJobPostingLdJson": bool(extraction.has_jobposting_ldjson),
    }
    try:
        data = await api.extract_validate(payload)
    except ApiError:
        return unchecked(extraction, "The AI check was unavailable; these are the page's own values.")

    confidence = data.get("confidence")
    return JobCheck(
        role=str(data.get("role") or "").strip()[:160],
        company=str(data.get("company") or "").strip()[:160],
        is_job_posting=bool(data.get("isJobPosting", True)),
        has_job_description=bool(data.get("hasJobDescription", True)),
        confidence=float(confidence) if isinstance(confidence, int | float) else None,
        notes=str(data.get("notes") or ""),
        role_corrected=bool(data.get("roleCorrected")),
        company_corrected=bool(data.get("companyCorrected")),
        source=str(data.get("source") or "fallback"),
    )
