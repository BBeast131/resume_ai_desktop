"""Official job APIs of the common applicant tracking systems.

When the real job page is hosted by Greenhouse, Lever, Ashby, Workday,
SmartRecruiters, Workable or Recruitee, the posting is also published as JSON
on a public, unauthenticated endpoint the boards themselves use. That copy is
the most reliable source there is: the exact title, the employer's own name
(for most of them) and the whole description, independent of how the page
renders. These are plain GET requests with no cookies, the same data any
visitor's browser loads.

Nothing here is logged except provider names, hosts, status codes and lengths.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from app.automation.htmltext import html_to_text

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 8.0
MAX_RESPONSE_BYTES = 5_000_000
MAX_DESCRIPTION_CHARS = 50_000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(frozen=True)
class AtsRef:
    """Where a posting's JSON lives."""

    provider: str
    page_url: str
    api_url: str
    #: A second request for the employer's name, when the posting does not carry it.
    company_url: str | None = None
    #: The board's slug, used (untrusted) when no real name is available.
    slug: str = ""
    #: For boards whose API lists every job (Ashby): the job to pick.
    job_id: str = ""


@dataclass(frozen=True)
class AtsPosting:
    provider: str
    title: str
    company: str
    #: True when the name came from the provider's data, False when it is only the board's slug.
    company_trusted: bool
    description: str
    url: str


def _pretty_slug(slug: str) -> str:
    words = re.split(r"[-_.]+", slug.strip())
    return " ".join(word.capitalize() for word in words if word)[:160]


def _segments(path: str) -> list[str]:
    return [part for part in path.split("/") if part]


_LANG_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def parse_ats_url(url: str) -> AtsRef | None:
    """The API address for a job page URL, or None when it is not a known ATS posting."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    segs = _segments(parts.path)
    query = parse_qs(parts.query)

    # Greenhouse: boards.greenhouse.io/{board}/jobs/{id}, job-boards.(eu.)greenhouse.io/..., embed/job_app?for=&token=
    if host.endswith("greenhouse.io"):
        board = job = ""
        if "for" in query and "token" in query:
            board, job = query["for"][0], query["token"][0]
        elif len(segs) >= 3 and segs[1] == "jobs":
            board, job = segs[0], segs[2]
        if board and job.isdigit():
            base = f"https://boards-api.greenhouse.io/v1/boards/{quote(board)}"
            return AtsRef("greenhouse", url, f"{base}/jobs/{job}", f"{base}", board, job)
        return None

    # Lever: jobs.lever.co/{company}/{uuid}[/apply]
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and len(segs) >= 2 and _UUID_RE.match(segs[1]):
        api_host = "api.eu.lever.co" if ".eu." in host else "api.lever.co"
        return AtsRef(
            "lever", url, f"https://{api_host}/v0/postings/{quote(segs[0])}/{segs[1]}", None, segs[0], segs[1]
        )

    # Ashby: jobs.ashbyhq.com/{org}/{uuid}[/application]
    if host == "jobs.ashbyhq.com" and len(segs) >= 2 and _UUID_RE.match(segs[1]):
        api = f"https://api.ashbyhq.com/posting-api/job-board/{quote(segs[0])}?includeCompensation=false"
        return AtsRef("ashby", url, api, None, segs[0], segs[1])

    # Workday: {tenant}.wdN.myworkdayjobs.com/[lang/]{site}/job/...
    #      or wdN.myworkdaysite.com/[lang/]recruiting/{tenant}/{site}/job/...
    if host.endswith("myworkdayjobs.com") or host.endswith("myworkdaysite.com"):
        rest = list(segs)
        if rest and _LANG_RE.match(rest[0]):
            rest = rest[1:]
        tenant = site = ""
        if host.endswith("myworkdaysite.com"):
            if len(rest) >= 3 and rest[0] == "recruiting":
                tenant, site, rest = rest[1], rest[2], rest[3:]
        else:
            tenant = host.split(".")[0]
            if rest:
                site, rest = rest[0], rest[1:]
        if tenant and site and rest and rest[0] in ("job", "details") and len(rest) >= 2:
            path = "/".join(quote(part) for part in rest[1:])
            api = f"https://{host}/wday/cxs/{quote(tenant)}/{quote(site)}/job/{path}"
            return AtsRef("workday", url, api, None, tenant, rest[-1])
        return None

    # SmartRecruiters: jobs.smartrecruiters.com/{company}/{id}[-slug]
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and len(segs) >= 2:
        match = re.match(r"^(\d{6,})", segs[1])
        if match:
            api = f"https://api.smartrecruiters.com/v1/companies/{quote(segs[0])}/postings/{match.group(1)}"
            return AtsRef("smartrecruiters", url, api, None, segs[0], match.group(1))
        return None

    # Workable: apply.workable.com/{account}/j/{shortcode}
    if host == "apply.workable.com" and len(segs) >= 3 and segs[1] == "j":
        base = f"https://apply.workable.com/api/v1/accounts/{quote(segs[0])}"
        api = f"https://apply.workable.com/api/v2/accounts/{quote(segs[0])}/jobs/{quote(segs[2])}"
        return AtsRef("workable", url, api, base, segs[0], segs[2])

    # Recruitee: {company}.recruitee.com/o/{slug}
    if host.endswith(".recruitee.com") and len(segs) >= 2 and segs[0] == "o":
        company = host.split(".")[0]
        return AtsRef("recruitee", url, f"https://{host}/api/offers/{quote(segs[1])}", None, company, segs[1])

    return None


def _text(value: Any) -> str:
    return " ".join(value.split())[:300] if isinstance(value, str) else ""


def _join(*parts: tuple[str, str]) -> str:
    """Description pieces as "Heading\\nbody" blocks, skipping empty ones."""
    blocks = []
    for heading, body in parts:
        body = body.strip()
        if body:
            blocks.append(f"{heading}\n{body}" if heading else body)
    return "\n\n".join(blocks)[:MAX_DESCRIPTION_CHARS]


def _parse(ref: AtsRef, data: Any, company_data: Any) -> AtsPosting | None:
    company = ""
    trusted = False
    title = ""
    description = ""

    if ref.provider == "greenhouse" and isinstance(data, dict):
        title = _text(data.get("title"))
        description = html_to_text(str(data.get("content") or ""))
        company = _text(data.get("company_name")) or (
            _text(company_data.get("name")) if isinstance(company_data, dict) else ""
        )
        trusted = bool(company)

    elif ref.provider == "lever" and isinstance(data, dict):
        title = _text(data.get("text"))
        lists: list[Any] = data["lists"] if isinstance(data.get("lists"), list) else []
        description = _join(
            ("", str(data.get("descriptionPlain") or "") or html_to_text(str(data.get("description") or ""))),
            *(
                (_text(item.get("text")), html_to_text(f"<ul>{item.get('content') or ''}</ul>"))
                for item in lists
                if isinstance(item, dict)
            ),
            ("", str(data.get("additionalPlain") or "") or html_to_text(str(data.get("additional") or ""))),
        )

    elif ref.provider == "ashby" and isinstance(data, dict):
        jobs: list[Any] = data["jobs"] if isinstance(data.get("jobs"), list) else []
        job = next((item for item in jobs if isinstance(item, dict) and str(item.get("id")) == ref.job_id), None)
        if job is None:
            return None
        title = _text(job.get("title"))
        description = str(job.get("descriptionPlain") or "") or html_to_text(str(job.get("descriptionHtml") or ""))

    elif ref.provider == "workday" and isinstance(data, dict):
        info: dict[str, Any] = data["jobPostingInfo"] if isinstance(data.get("jobPostingInfo"), dict) else {}
        title = _text(info.get("title"))
        description = html_to_text(str(info.get("jobDescription") or ""))
        organization = data.get("hiringOrganization")
        company = _text(organization.get("name")) if isinstance(organization, dict) else ""
        trusted = bool(company)

    elif ref.provider == "smartrecruiters" and isinstance(data, dict):
        title = _text(data.get("name"))
        organization = data.get("company")
        company = _text(organization.get("name")) if isinstance(organization, dict) else ""
        trusted = bool(company)
        sections = ((data.get("jobAd") or {}).get("sections") or {}) if isinstance(data.get("jobAd"), dict) else {}
        pieces = []
        for key in ("jobDescription", "qualifications", "additionalInformation", "companyDescription"):
            section = sections.get(key) if isinstance(sections, dict) else None
            if isinstance(section, dict):
                pieces.append((_text(section.get("title")), html_to_text(str(section.get("text") or ""))))
        description = _join(*pieces)

    elif ref.provider == "workable" and isinstance(data, dict):
        title = _text(data.get("title"))
        description = _join(
            ("", html_to_text(str(data.get("description") or ""))),
            ("Requirements", html_to_text(str(data.get("requirements") or ""))),
            ("Benefits", html_to_text(str(data.get("benefits") or ""))),
        )
        company = _text(company_data.get("name")) if isinstance(company_data, dict) else ""
        trusted = bool(company)

    elif ref.provider == "recruitee" and isinstance(data, dict):
        offer: dict[str, Any] = data["offer"] if isinstance(data.get("offer"), dict) else data
        title = _text(offer.get("title"))
        description = _join(
            ("", html_to_text(str(offer.get("description") or ""))),
            ("Requirements", html_to_text(str(offer.get("requirements") or ""))),
        )
        company = _text(offer.get("company_name"))
        trusted = bool(company)

    if not title or len(description.strip()) < 100:
        return None
    if not company:
        company = _pretty_slug(ref.slug)
        trusted = False
    return AtsPosting(
        provider=ref.provider,
        title=title[:160],
        company=company[:160],
        company_trusted=trusted,
        description=description[:MAX_DESCRIPTION_CHARS].strip(),
        url=ref.page_url,
    )


async def _get_json(http: httpx.AsyncClient, url: str) -> Any:
    try:
        response = await http.get(url)
    except (httpx.HTTPError, ValueError) as error:
        log.info("ats.fetch_failed host=%s error=%s", urlsplit(url).hostname, type(error).__name__)
        return None
    if response.status_code != 200 or len(response.content) > MAX_RESPONSE_BYTES:
        log.info("ats.fetch_status host=%s status=%s", urlsplit(url).hostname, response.status_code)
        return None
    try:
        return response.json()
    except ValueError:
        return None


async def fetch_posting(ref: AtsRef, http: httpx.AsyncClient) -> AtsPosting | None:
    data = await _get_json(http, ref.api_url)
    if data is None:
        return None
    company_data = None
    needs_company = ref.company_url and not (
        ref.provider == "greenhouse" and isinstance(data, dict) and data.get("company_name")
    )
    if needs_company and ref.company_url:
        company_data = await _get_json(http, ref.company_url)
    posting = _parse(ref, data, company_data)
    log.info(
        "ats.posting provider=%s found=%s jd_len=%s",
        ref.provider,
        posting is not None,
        len(posting.description) if posting else 0,
    )
    return posting


def candidate_refs(urls: Iterable[str]) -> list[AtsRef]:
    refs: list[AtsRef] = []
    seen: set[str] = set()
    for url in urls:
        ref = parse_ats_url(url) if isinstance(url, str) else None
        if ref and ref.api_url not in seen:
            seen.add(ref.api_url)
            refs.append(ref)
    return refs


async def lookup(urls: Iterable[str], http: httpx.AsyncClient | None = None) -> AtsPosting | None:
    """The first of these URLs that is a known ATS posting with a readable API copy."""
    refs = candidate_refs(urls)[:3]
    if not refs:
        return None
    own = http is None
    client = http or httpx.AsyncClient(
        timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=5.0),
        follow_redirects=True,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        for ref in refs:
            posting = await fetch_posting(ref, client)
            if posting is not None:
                return posting
        return None
    finally:
        if own:
            await client.aclose()
