"""Deciding a job's role, company and description from several sources.

A job is read from up to five kinds of evidence, each with its own reliability:

=====================  =======================================================  ======
source                 what it is                                               trust
=====================  =======================================================  ======
``ats``                the ATS's official job API (Greenhouse, Workday, ...)    1.00
``net``                a JSON response the page itself loaded (its own API)     0.85
``ldjson``             schema.org JobPosting embedded in the page               0.85
``state``              app data the page was rendered from (__NEXT_DATA__...)   0.75
``listing``            role/company saved from the job board's result card      0.70
``adapter``            a known site's markup                                    0.65
``dom``                generic reading of the page text                         0.45
=====================  =======================================================  ======

Role and company are each decided by agreement: values that mean the same
thing (case, punctuation, "Sr."/"Senior", "Inc."...) are grouped and the group
with the most combined trust wins; its value is taken from its most trusted
member. Values that are obviously not an employer or a job title (an ATS's own
name, "Careers", "Apply now") never count.

The description is the most trustworthy *complete* text: every text at least
60% as long as the longest one found counts as complete, and of those the most
trusted wins (an API copy over a layout reading), so a 300-character teaser
does not beat the full posting. Structured candidates about a different job (a "similar jobs" list
in the page data) are dropped when they match none of the titles seen.

Each decision comes with a confidence in [0, 1] and the source it came from,
which the pipeline uses to decide whether the AI check may overrule it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.services.dedupe import company_key, role_key

Field = Literal["role", "company"]

TRUST: dict[str, float] = {
    "ats": 1.0,
    "net": 0.85,
    "ldjson": 0.85,
    "state": 0.75,
    "listing": 0.70,
    "adapter": 0.65,
    "dom": 0.45,
}
#: Trust of an ATS company that is only the board's slug ("acme-corp" -> "Acme Corp").
SLUG_COMPANY_TRUST = 0.35

#: A role or company decided with at least this confidence stands against the AI check.
STRONG_CONFIDENCE = 0.8

MIN_JD_CHARS = 100

_PLATFORM_NAMES = frozenset(
    company_key(name)
    for name in (
        "greenhouse", "lever", "workday", "myworkdayjobs", "ashby", "ashbyhq", "smartrecruiters", "workable",
        "recruitee", "icims", "jobvite", "taleo", "bamboohr", "breezy", "jazzhr", "jobright", "jobright ai",
        "hiring cafe", "hiringcafe", "linkedin", "indeed", "glassdoor", "ziprecruiter", "monster", "dice",
        "careers", "career", "jobs", "job board", "job", "home", "apply", "builtin", "built in", "wellfound",
        "simplify", "otta", "untitled",
    )
)  # fmt: skip
_BAD_ROLES = frozenset(
    role_key(text)
    for text in (
        "apply", "apply now", "job details", "job detail", "job description", "careers", "jobs", "home",
        "search jobs", "job search", "open positions", "current openings", "sign in", "log in", "untitled",
    )
)  # fmt: skip
_JD_WORDS = (
    "responsibilit", "requirement", "qualification", "experience", "about the role", "about you",
    "what you", "we are looking", "benefits", "skills", "you will", "you'll", "preferred",
)  # fmt: skip


@dataclass(frozen=True)
class Evidence:
    source: str
    role: str = ""
    company: str = ""
    description: str = ""
    #: Overrides the trust of the company value (an ATS slug, say).
    company_trust: float | None = None
    #: Where it came from, for the log and the review panel (a host or an API name).
    origin: str = ""
    #: Added to the source's trust: the real job site counts a little more than an aggregator's copy.
    boost: float = 0.0

    @property
    def trust(self) -> float:
        return TRUST.get(self.source, 0.4) + self.boost


@dataclass
class Decision:
    value: str = ""
    source: str = ""
    confidence: float = 0.0


@dataclass
class FusedJob:
    role: Decision = field(default_factory=Decision)
    company: Decision = field(default_factory=Decision)
    jd: Decision = field(default_factory=Decision)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """For the run log: sources and lengths only, never the text."""
        return (
            f"role: {self.role.source or '-'} ({self.role.confidence:.2f}), "
            f"company: {self.company.source or '-'} ({self.company.confidence:.2f}), "
            f"description: {self.jd.source or '-'} ({len(self.jd.value)} chars)"
        )


def valid_company(value: str) -> bool:
    key = company_key(value)
    return 2 <= len(value.strip()) <= 160 and bool(key) and key not in _PLATFORM_NAMES


def valid_role(value: str, company: str = "") -> bool:
    key = role_key(value)
    if not (2 <= len(value.strip()) <= 160) or not key or key in _BAD_ROLES:
        return False
    if company and company_key(value) == company_key(company):
        return False
    return not value.strip().lower().startswith(("http://", "https://"))


def same_role(a: str, b: str) -> bool:
    left, right = set(role_key(a).split()), set(role_key(b).split())
    if not left or not right:
        return False
    if left == right:
        return True
    smaller, larger = (left, right) if len(left) <= len(right) else (right, left)
    # "Data Engineer" vs "Senior Data Engineer, Platform": every word of the shorter one present.
    if len(smaller) >= 2 and smaller <= larger:
        return True
    return len(left & right) / len(left | right) >= 0.75


def same_company(a: str, b: str) -> bool:
    left, right = company_key(a), company_key(b)
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    # "Acme" vs "Acme Health": the shorter name starts the longer one.
    return len(shorter) >= 4 and (
        longer.startswith(shorter + " ") or longer.replace(" ", "") == shorter.replace(" ", "")
    )


def _decide(items: list[tuple[str, float, str]], same: object, total_floor: float = 1.0) -> Decision:
    """items: (value, weight, source). Groups agreeing values; the heaviest group wins."""
    groups: list[list[tuple[str, float, str]]] = []
    for item in items:
        for group in groups:
            if same(item[0], group[0][0]):  # type: ignore[operator]
                group.append(item)
                break
        else:
            groups.append([item])
    if not groups:
        return Decision()
    weights = [sum(weight for _, weight, _ in group) for group in groups]
    best_index = max(range(len(groups)), key=lambda index: weights[index])
    best = groups[best_index]
    value, _, source = max(best, key=lambda item: item[1])
    total = sum(weights)
    agreement = weights[best_index] / total if total else 0.0
    confidence = min(1.0, weights[best_index] / total_floor) * (agreement**0.5)
    return Decision(value=value.strip(), source=source, confidence=round(confidence, 3))


def _jd_score(text: str, trust: float) -> float:
    length = len(text)
    lower = text[:20000].lower()
    hits = sum(1 for word in _JD_WORDS if word in lower)
    score = trust * 0.6 + 0.3 * min(length / 2500, 1.0) + 0.1 * min(hits / 4, 1.0)
    if length < 500:
        score -= 0.25
    if length > 40_000:
        score -= 0.1  # most likely a whole page, not one posting
    return score


def fuse(evidence: list[Evidence]) -> FusedJob:
    """Decide role, company and description from all the evidence there is."""
    result = FusedJob()
    items = [item for item in evidence if item is not None]

    # Page data often holds several jobs (the posting plus "similar jobs"). Titles from
    # reliable sources say which one this is; a generic page reading only breaks a tie.
    structured = [item for item in items if item.source in ("net", "state")]
    strong = [
        item.role for item in items if item.source in ("ats", "listing", "ldjson", "adapter") and valid_role(item.role)
    ]
    weak = [item.role for item in items if item.source == "dom" and valid_role(item.role)]
    distinct = {role_key(item.role) for item in structured}
    anchors = strong or (weak if len(distinct) > 1 else [])
    usable: list[Evidence] = []
    for item in items:
        if item in structured and anchors and not any(same_role(item.role, anchor) for anchor in anchors):
            result.notes.append(f"ignored {item.source} data about another job ({item.origin or 'page'})")
            continue
        usable.append(item)

    companies = [
        (item.company, item.company_trust if item.company_trust is not None else item.trust, item.source)
        for item in usable
        if item.company and valid_company(item.company)
    ]
    result.company = _decide(companies, same_company)

    roles = [
        (item.role, item.trust, item.source)
        for item in usable
        if item.role and valid_role(item.role, result.company.value)
    ]
    result.role = _decide(roles, same_role)

    texts = [item for item in usable if len(item.description.strip()) >= MIN_JD_CHARS]
    if texts:
        longest = max(len(item.description.strip()) for item in texts)
        # Complete enough: not a teaser of a much longer text found elsewhere.
        floor = max(500, int(longest * 0.6)) if longest >= 500 else MIN_JD_CHARS
        complete = [item for item in texts if len(item.description.strip()) >= floor]
        # Of the complete ones, the most trusted (an API copy over a layout reading); then the longer.
        chosen = max(complete, key=lambda item: (round(item.trust, 3), len(item.description)))
        score = _jd_score(chosen.description.strip(), chosen.trust)
        result.jd = Decision(
            value=chosen.description.strip(), source=chosen.source, confidence=round(min(1.0, max(0.0, score)), 3)
        )
    return result


class PageReading(Protocol):
    """What `app.automation.extractor.Extraction` offers (kept structural to avoid a Qt import)."""

    @property
    def url(self) -> str: ...
    @property
    def role(self) -> str: ...
    @property
    def company(self) -> str: ...
    @property
    def jd_text(self) -> str: ...
    @property
    def source(self) -> str: ...
    @property
    def candidates(self) -> Sequence[Any]: ...
    @property
    def has_jd(self) -> bool: ...


def page_evidence(page: PageReading, *, boost: float = 0.0) -> list[Evidence]:
    """One page reading as evidence: its own fields plus every structured candidate it found."""
    if page.source == "ldjson":
        kind = "ldjson"
    elif page.source.startswith("adapter:"):
        kind = "adapter"
    else:
        kind = "dom"
    host = _host(page.url)
    out = [
        Evidence(
            kind,
            role=page.role,
            company=page.company,
            description=page.jd_text if page.has_jd else "",
            origin=host,
            boost=boost,
        )
    ]
    for candidate in page.candidates:
        out.append(
            Evidence(
                candidate.source,
                role=candidate.title,
                company=candidate.company,
                description=candidate.description,
                origin=candidate.origin or host,
                boost=boost,
            )
        )
    return out


def ats_evidence(posting: Any) -> Evidence:
    """An `ats_api.AtsPosting` as evidence; a slug-only company counts for little."""
    return Evidence(
        "ats",
        role=posting.title,
        company=posting.company,
        description=posting.description,
        company_trust=None if posting.company_trusted else SLUG_COMPANY_TRUST,
        origin=posting.provider,
    )


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
