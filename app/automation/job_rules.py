"""Rules about job pages that need no browser: which site a URL is on, when
two URLs are the same posting, what "form-only" means, and whether a page the
user opened during Job Search gets saved.

Pure Python on purpose. Nothing here imports Qt, so the rules can be tested
without a page and reused by the pipeline, the capture code and the store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import parse_qsl, unquote, urlsplit

Site = Literal["jobright", "hiring_cafe", "linkedin", "chatgpt", "other"]

# ---------------------------------------------------------------------------
# Blocked pages
# ---------------------------------------------------------------------------

#: Why a page cannot be read without a person, in the words the UI shows.
#: The keys are the reason codes `extractor.js` returns, plus `timeout`.
BLOCK_MESSAGES: dict[str, str] = {
    "captcha": "page asks to verify you are human",
    "login": "page needs sign-in",
    "consent": "page is behind a consent wall",
    "http_error": "page is not available",
    "timeout": "page took too long to load",
    "empty": "no job details could be read",
}

#: A job description shorter than this is not a job description (spec section 3).
MIN_JD_CHARS = 100

# ---------------------------------------------------------------------------
# Which site
# ---------------------------------------------------------------------------

_SITE_HOSTS: tuple[tuple[Site, tuple[str, ...]], ...] = (
    ("jobright", ("jobright.ai",)),
    ("hiring_cafe", ("hiring.cafe", "hiringcafe.com")),
    ("linkedin", ("linkedin.com", "lnkd.in")),
    ("chatgpt", ("chatgpt.com", "chat.openai.com")),
)


def _host(url: str) -> str:
    """The lowercased host of an http(s) URL without `www.`, or ''."""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _host_is(host: str, domain: str) -> bool:
    # The host IS the domain or a subdomain of it. `jobright.ai.evil.example`
    # merely starts with it and is someone else's site.
    return host == domain or host.endswith("." + domain)


def site_of(url: str) -> Site:
    """Which of the sites the app knows about a URL belongs to."""
    host = _host(url)
    if not host:
        return "other"
    for site, domains in _SITE_HOSTS:
        if any(_host_is(host, domain) for domain in domains):
            return site
    return "other"


# jobright's own page for one job: jobright.ai/jobs/info/<id>. The other shapes
# are tolerated because the site's routes could not be inspected when this was
# written; a list or home page never matches.
_JOBRIGHT_DETAIL_PATH = re.compile(r"^/(?:jobs?/(?:info|detail|details|view)|job)/[^/]+/?$", re.IGNORECASE)
_JOBRIGHT_ID_PARAMS = frozenset({"jobid", "job_id"})


def is_jobright_detail_url(url: str) -> bool:
    """Is this jobright.ai's internal detail page for a single job?"""
    if site_of(url) != "jobright":
        return False
    parts = urlsplit(url.strip())
    if _JOBRIGHT_DETAIL_PATH.match(parts.path):
        return True
    return any(key.lower() in _JOBRIGHT_ID_PARAMS and value for key, value in parse_qsl(parts.query))


# ---------------------------------------------------------------------------
# url_key: the URL reduced to what identifies the job
# ---------------------------------------------------------------------------

# A BLOCKLIST, deliberately (ported from the web app's job-matching.ts). An
# unknown tracking parameter we fail to strip makes ONE posting look like two,
# which costs a duplicate. An unknown identifying parameter that gets dropped
# makes TWO postings look like one, which locks a job out entirely. So
# everything not known to be noise is kept: gh_jid, jobId, job_id, jk, id,
# for, token and whatever else a site uses.
_TRACKING_KEYS = frozenset(
    {
        # Universal campaign tagging (utm_* is matched by prefix below).
        "gclid",
        "fbclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "igshid",
        # Generic referral noise.
        "ref",
        "referer",
        "referrer",
        "src",
        "source",
        "from",
        "campaign",
        "medium",
        # LinkedIn.
        "trk",
        "trackingid",
        "refid",
        "original_referer",
        "originalsubdomain",
        "position",
        "pagenum",
        "ebp",
        "savedsearchid",
        "recommendedflavor",
        # Indeed. `jk` is the posting id and is deliberately NOT here.
        "tk",
        "vjs",
        "advn",
        "adid",
        "sjdu",
        "xkcb",
        "xpse",
        "alid",
        # Greenhouse / Lever source tagging. `gh_jid`, `for` and `token`
        # identify the posting and are deliberately NOT here.
        "gh_src",
        "lever-source",
        "lever-origin",
    }
)
_TRACKING_PREFIXES = ("utm_",)

_GREENHOUSE_HOSTS = frozenset(
    {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "boards.eu.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }
)
_GREENHOUSE_BOARD = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_GREENHOUSE_JOB = re.compile(r"^\d{1,20}$")
_LEVER_COMPANY = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_ASHBY_ORG = re.compile(r"^[A-Za-z0-9._ -]{1,100}$")
_WORKABLE_SHORTCODE = re.compile(r"^[A-Za-z0-9]{6,20}$")
# The application step of a posting is the same job as the posting.
_APPLY_STEP = re.compile(r"/(apply|application|applications|apply-now|form)$", re.IGNORECASE)


def _is_tracking_key(key: str) -> bool:
    return key in _TRACKING_KEYS or key.startswith(_TRACKING_PREFIXES)


def _greenhouse_key(board: str, job_id: str) -> str | None:
    if _GREENHOUSE_BOARD.match(board) and _GREENHOUSE_JOB.match(job_id):
        return f"greenhouse:{board.lower()}:{job_id}"
    return None


def _ats_key(host: str, segments: list[str], query: dict[str, str]) -> str | None:
    """The job's own id on a known applicant-tracking system (ats-job-ref.ts)."""
    if host in _GREENHOUSE_HOSTS:
        token = query.get("token") or query.get("gh_jid")
        board = query.get("for")
        # /embed/job_app?for=<board>&token=<id>
        if segments[:1] == ["embed"] and board and token:
            return _greenhouse_key(board, token)
        # /<board>/jobs/<id>
        if len(segments) >= 3 and segments[1] == "jobs" and segments[0] != "embed":
            return _greenhouse_key(segments[0], segments[2])
        # /<board>?gh_jid=<id>
        if segments and segments[0] != "embed" and token:
            return _greenhouse_key(segments[0], token)
        return None

    if host in ("jobs.lever.co", "jobs.eu.lever.co"):
        if len(segments) >= 2 and _LEVER_COMPANY.match(segments[0]) and _UUID.match(segments[1]):
            return f"lever:{segments[0].lower()}:{segments[1].lower()}"
        return None

    if host == "jobs.ashbyhq.com":
        if not segments:
            return None
        job_id = segments[1] if len(segments) >= 2 and _UUID.match(segments[1]) else query.get("ashby_jid", "")
        if job_id and _ASHBY_ORG.match(segments[0]) and _UUID.match(job_id):
            return f"ashby:{segments[0].lower()}:{job_id.lower()}"
        return None

    if host == "apply.workable.com" or host.endswith(".workable.com"):
        # apply.workable.com/<account>/j/<code>/…, <account>.workable.com/j/<code>
        if "j" in segments:
            index = segments.index("j")
            if index + 1 < len(segments) and _WORKABLE_SHORTCODE.match(segments[index + 1]):
                return f"workable:{segments[index + 1].upper()}"
        return None

    return None


def url_key(url: str) -> str:
    """The URL reduced to what identifies the job, for "already saved" checks.

    Two URLs of the same posting give the same key: the host is lowercased
    without `www.`, tracking parameters and the fragment are dropped, the
    remaining parameters are sorted, a trailing slash and a trailing apply
    step (`/apply`, `/application`) are removed. On Greenhouse, Lever, Ashby
    and Workable the key is the ATS's own job id.

    Anything that is not an http(s) URL comes back trimmed and unchanged, so
    two different unusable values never collapse into one key.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname or ""
        port = parts.port
    except ValueError:
        return raw
    if parts.scheme.lower() not in ("http", "https") or not hostname:
        return raw

    full_host = hostname.lower().rstrip(".")
    host = full_host[4:] if full_host.startswith("www.") else full_host

    # Sorted, so the order parameters arrive in cannot matter.
    kept = sorted(
        (key.lower(), value)
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if not _is_tracking_key(key.lower())
    )
    query = dict(reversed(kept))  # a repeated key resolves the same way whatever the order

    segments = [unquote(segment) for segment in parts.path.split("/") if segment]
    ats = _ats_key(full_host, segments, query)
    if ats:
        return ats

    # Case in the path is not identity: boards routinely serve one posting at
    # differing case.
    path = _APPLY_STEP.sub("", re.sub(r"/+$", "", parts.path).lower())

    # A fragment is dropped unless it is the route itself ("#/jobs/123").
    fragment = parts.fragment
    route = f"#{fragment.rstrip('/')}" if fragment.startswith(("/", "!/")) and len(fragment) > 2 else ""

    default_port = port is None or (parts.scheme.lower(), port) in (("http", 80), ("https", 443))
    netloc = host if default_port else f"{host}:{port}"
    query_text = "?" + "&".join(f"{key}={value}" for key, value in kept) if kept else ""
    return f"{netloc}{path}{query_text}{route}"


# ---------------------------------------------------------------------------
# Form-only pages
# ---------------------------------------------------------------------------

FORM_ONLY_PROSE_MAX = 600
FORM_ONLY_MIN_FIELDS = 3


def is_form_only(has_ldjson: bool, prose_len: int, form_field_count: int) -> bool:
    """An application form with no job description around it.

    No JobPosting LD-JSON, AND fewer than ~600 characters of descriptive prose
    outside form controls, AND at least 3 form fields. The Groq check can
    overrule it.
    """
    return not has_ldjson and prose_len < FORM_ONLY_PROSE_MAX and form_field_count >= FORM_ONLY_MIN_FIELDS


# ---------------------------------------------------------------------------
# Job Search capture: is this page saved?
# ---------------------------------------------------------------------------


class ExtractionLike(Protocol):
    """What `decide_capture` reads. `app.automation.extractor.Extraction` fits."""

    @property
    def url(self) -> str: ...
    @property
    def role(self) -> str: ...
    @property
    def company(self) -> str: ...
    @property
    def jd_text(self) -> str: ...
    @property
    def is_linkedin(self) -> bool: ...
    @property
    def linkedin_source(self) -> bool: ...
    @property
    def blocked(self) -> bool: ...
    @property
    def block_reason(self) -> str | None: ...
    @property
    def form_only(self) -> bool: ...
    @property
    def has_jd(self) -> bool: ...


CaptureCode = Literal["saved", "linkedin", "form_only_no_jd", "needs_user_action", "not_a_job"]

TOAST_LINKEDIN = "Skipped: LinkedIn job"
TOAST_NO_JD = "Skipped: no job description"
TOAST_NOT_A_JOB = "Skipped: not a job page"
TOAST_FROM_JOBRIGHT = "Skipped: save the JobRight page instead"
TOAST_DUPLICATE = "Already saved"


@dataclass(frozen=True)
class CaptureDecision:
    #: Save the page as a job (still subject to the Groq check and dedupe).
    save: bool
    #: `saved`, `linkedin`, `form_only_no_jd`, `needs_user_action` or `not_a_job`.
    code: str
    #: What the toast says. For `saved` it is built from the extractor's role
    #: and company; rebuild it with `saved_toast` once Groq has confirmed them.
    toast: str
    #: jobright's own description, kept because the real site may be a bare form.
    jd_text: str | None


def saved_toast(role: str, company: str) -> str:
    """The toast for a saved job, e.g. `Saved: Staff Engineer at Acme`."""
    role = " ".join(role.split())
    company = " ".join(company.split())
    if role and company:
        return f"Saved: {role} at {company}"
    return f"Saved: {role or company or 'job'}"


def blocked_toast(reason: str | None) -> str:
    """The toast for a page that needs a person, e.g. `Not saved: page needs sign-in`."""
    return f"Not saved: {BLOCK_MESSAGES.get(reason or '', BLOCK_MESSAGES['empty'])}"


def decide_capture(extraction: ExtractionLike, opener_site: str) -> CaptureDecision:
    """The save rules for a tab the user opened from Job Search.

    `opener_site` is the site of the tab the click came from (`site_of` of its
    URL). The rules, in order:

    1. A tab on linkedin.com is never saved, whatever opened it.
    2. A jobright.ai page: only the internal job detail page is a job. If it
       shows that the job's original source is LinkedIn it is not saved;
       otherwise it is saved with jobright's own description.
    3. A real job site reached from a jobright page is not saved: the jobright
       detail page already stands for that job, and the pipeline opens the
       real site itself.
    4. Any other page (hiring.cafe to the real job site): a page that needs a
       person is not saved and says why; a form with no description is not
       saved; a page with a real description is saved.
    """
    page_site = site_of(extraction.url)

    if extraction.is_linkedin or page_site == "linkedin":
        return CaptureDecision(False, "linkedin", TOAST_LINKEDIN, None)

    if page_site == "chatgpt":
        return CaptureDecision(False, "not_a_job", TOAST_NOT_A_JOB, None)

    if page_site == "jobright":
        if extraction.linkedin_source:
            return CaptureDecision(False, "linkedin", TOAST_LINKEDIN, None)
        if not is_jobright_detail_url(extraction.url):
            return CaptureDecision(False, "not_a_job", TOAST_NOT_A_JOB, None)
        if extraction.blocked:
            return CaptureDecision(False, "needs_user_action", blocked_toast(extraction.block_reason), None)
        jd_text = extraction.jd_text.strip()
        return CaptureDecision(True, "saved", saved_toast(extraction.role, extraction.company), jd_text or None)

    if opener_site == "jobright":
        return CaptureDecision(False, "not_a_job", TOAST_FROM_JOBRIGHT, None)

    if page_site == "hiring_cafe" and extraction.linkedin_source:
        return CaptureDecision(False, "linkedin", TOAST_LINKEDIN, None)

    # A bare application form is "no job description", not a page that needs a
    # person, even though nothing could be read from it either.
    if extraction.form_only and not extraction.has_jd and extraction.block_reason in (None, "empty"):
        return CaptureDecision(False, "form_only_no_jd", TOAST_NO_JD, None)

    if extraction.blocked:
        return CaptureDecision(False, "needs_user_action", blocked_toast(extraction.block_reason), None)

    if not extraction.has_jd:
        return CaptureDecision(False, "not_a_job", TOAST_NO_JD, None)

    return CaptureDecision(True, "saved", saved_toast(extraction.role, extraction.company), None)
