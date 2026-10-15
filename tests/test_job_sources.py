"""Reading a job from several sources: ATS APIs, the page's own JSON, the DOM, and deciding between them."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import QWebEnginePage

from app.automation import ats_api, extractor
from app.automation.ats_api import AtsPosting, parse_ats_url
from app.automation.evidence import (
    STRONG_CONFIDENCE,
    Evidence,
    ats_evidence,
    fuse,
    page_evidence,
    same_company,
    same_role,
    valid_company,
    valid_role,
)
from app.automation.extractor import Extraction, StructuredCandidate, extract_page
from app.automation.htmltext import html_to_text
from app.browser.jsbridge import ensure_script, run_js, run_js_json
from app.browser.profile import NETWORK_CAPTURE_NAME, install_network_capture
from tests.helpers import load_html

LONG_JD = (
    "About the role\nYou will build data pipelines for the analytics team and own their reliability.\n\n"
    "Responsibilities\n• Design and run batch and streaming pipelines\n• Work with product analysts\n\n"
    "Requirements\n• 5+ years of experience with Python and SQL\n• Experience with Spark or Flink\n\n"
    "Benefits\n• Health insurance\n• Remote friendly\n"
) * 3


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    monkeypatch.setattr(extractor, "SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(extractor, "EXPAND_SECONDS", 0.05)
    monkeypatch.setattr(extractor, "STABLE_MAX_SECONDS", 1.0)
    monkeypatch.setattr(extractor, "STABLE_QUIET_SECONDS", 0.2)


# ---------------------------------------------------------------------------
# Deciding between sources
# ---------------------------------------------------------------------------


def test_names_that_are_not_an_employer_or_a_title_never_count():
    for platform in ("Workday", "Greenhouse", "Careers", "jobs", "JobRight.ai", "LinkedIn"):
        assert not valid_company(platform)
    assert valid_company("Acme Health, Inc.")
    for bad in ("Apply now", "Job Details", "Careers", "https://acme.com/jobs/1"):
        assert not valid_role(bad)
    assert not valid_role("Acme", company="Acme Inc.")
    assert valid_role("Senior Data Engineer")


def test_values_that_mean_the_same_thing_are_grouped():
    assert same_role("Sr. Data Engineer", "senior data engineer")
    assert same_role("Data Engineer", "Senior Data Engineer, Platform")
    assert not same_role("Data Engineer", "Product Designer")
    assert same_company("Acme Inc.", "ACME")
    assert same_company("Acme", "Acme Health")
    assert not same_company("Acme", "Globex")


def test_the_ats_api_outranks_a_wrong_company_and_a_teaser_on_the_page():
    fused = fuse(
        [
            Evidence("listing", role="Senior Data Engineer", company="Acme Health"),
            # The real site read through its layout: the ATS's own name as company, a short teaser.
            Evidence("dom", role="Senior Data Engineer", company="Workday", description="Join us! " * 30),
            Evidence("ats", role="Senior Data Engineer", company="Acme Health", description=LONG_JD),
        ]
    )
    assert fused.company.value == "Acme Health" and fused.company.source == "ats"
    assert fused.company.confidence >= STRONG_CONFIDENCE
    assert fused.role.value == "Senior Data Engineer" and fused.role.confidence >= STRONG_CONFIDENCE
    assert fused.jd.source == "ats" and fused.jd.value == LONG_JD.strip()


def test_page_data_about_another_job_is_ignored():
    fused = fuse(
        [
            Evidence("listing", role="Data Engineer", company="Acme"),
            Evidence("state", role="Product Designer", company="Globex", description=LONG_JD + "x" * 3000),
            Evidence("net", role="Data Engineer", company="Acme", description=LONG_JD),
            Evidence("adapter", role="Data Engineer", company="Acme", description="Short bit about the role. " * 10),
        ]
    )
    assert fused.role.value == "Data Engineer"
    assert fused.company.value == "Acme"
    assert fused.jd.source == "net"
    assert any("another job" in note for note in fused.notes)


def test_a_lone_weak_reading_is_low_confidence_and_an_ats_slug_counts_for_little():
    weak = fuse([Evidence("dom", role="Data Engineer", company="Acme", description=LONG_JD)])
    assert weak.role.confidence < STRONG_CONFIDENCE and weak.company.confidence < STRONG_CONFIDENCE

    posting = AtsPosting("lever", "Data Engineer", "Acme Corp", False, LONG_JD, "https://jobs.lever.co/acme-corp/x")
    fused = fuse([Evidence("listing", role="Data Engineer", company="Acme Health"), ats_evidence(posting)])
    assert fused.company.value == "Acme Health"  # the listing (0.7) beats a slug (0.35)
    assert fused.role.confidence >= STRONG_CONFIDENCE


def test_the_longer_complete_description_wins_over_a_more_trusted_teaser():
    teaser = "We are hiring a data engineer to join the team. " * 6  # ~300 chars
    fused = fuse(
        [
            Evidence("ldjson", role="Data Engineer", company="Acme", description=teaser),
            Evidence("adapter", role="Data Engineer", company="Acme", description=LONG_JD),
        ]
    )
    assert fused.jd.source == "adapter"


def test_page_evidence_carries_the_dom_reading_and_every_structured_candidate():
    reading = Extraction(
        url="https://jobright.ai/jobs/info/1",
        role="Data Engineer",
        company="Acme",
        jd_text=LONG_JD,
        source="adapter:jobright",
        candidates=[StructuredCandidate("net", "jobright.ai", "Data Engineer", "Acme", LONG_JD, 3)],
    )
    items = page_evidence(reading, boost=0.1)
    assert [item.source for item in items] == ["adapter", "net"]
    assert items[0].origin == "jobright.ai" and items[0].trust == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# ATS APIs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "provider", "api"),
    [
        (
            "https://boards.greenhouse.io/acme/jobs/4012345",
            "greenhouse",
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs/4012345",
        ),
        (
            "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=4012345",
            "greenhouse",
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs/4012345",
        ),
        (
            "https://jobs.lever.co/acme/1b2c3d4e-0000-4000-8000-1234567890ab/apply",
            "lever",
            "https://api.lever.co/v0/postings/acme/1b2c3d4e-0000-4000-8000-1234567890ab",
        ),
        (
            "https://jobs.ashbyhq.com/acme/1b2c3d4e-0000-4000-8000-1234567890ab",
            "ashby",
            "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=false",
        ),
        (
            "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/Data-Engineer_R123",
            "workday",
            "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/job/Austin-TX/Data-Engineer_R123",
        ),
        (
            "https://wd3.myworkdaysite.com/en-US/recruiting/acme/Careers/job/Remote/Data-Engineer_R9",
            "workday",
            "https://wd3.myworkdaysite.com/wday/cxs/acme/Careers/job/Remote/Data-Engineer_R9",
        ),
        (
            "https://jobs.smartrecruiters.com/Acme/744000012345678-data-engineer",
            "smartrecruiters",
            "https://api.smartrecruiters.com/v1/companies/Acme/postings/744000012345678",
        ),
        (
            "https://apply.workable.com/acme/j/AB12CD34EF/",
            "workable",
            "https://apply.workable.com/api/v2/accounts/acme/jobs/AB12CD34EF",
        ),
        (
            "https://acme.recruitee.com/o/data-engineer",
            "recruitee",
            "https://acme.recruitee.com/api/offers/data-engineer",
        ),
    ],
)
def test_known_ats_addresses_map_to_their_api(url, provider, api):
    ref = parse_ats_url(url)
    assert ref is not None and ref.provider == provider and ref.api_url == api


def test_other_addresses_are_not_ats_postings():
    for url in (
        "https://jobright.ai/jobs/info/123",
        "https://boards.greenhouse.io/acme",
        "https://jobs.lever.co/acme",
        "https://acme.wd5.myworkdayjobs.com/en-US/External",
        "ftp://jobs.lever.co/acme/x",
        "not a url",
    ):
        assert parse_ats_url(url) is None


def test_html_from_apis_becomes_readable_text():
    escaped = "&lt;p&gt;About us&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;li&gt;SQL&lt;/li&gt;&lt;/ul&gt;"
    assert html_to_text(escaped) == "About us\n\n• Python\n• SQL"
    assert (
        html_to_text("<script>x()</script><h2>Role</h2><p>Build things.<br>Daily.</p>")
        == "Role\n\nBuild things.\nDaily."
    )
    assert html_to_text("Plain text &amp; more") == "Plain text & more"


def _client(routes: dict[str, Any], calls: list[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if url in routes:
            return httpx.Response(200, json=routes[url])
        return httpx.Response(404, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


JD_HTML = "<p>" + "Build reliable data pipelines with Python and SQL. " * 5 + "</p><ul><li>Spark</li></ul>"


async def test_greenhouse_posting_with_its_company_name():
    calls: list[str] = []
    base = "https://boards-api.greenhouse.io/v1/boards/acme"
    client = _client({f"{base}/jobs/7": {"title": "Data Engineer ", "company_name": "Acme", "content": JD_HTML}}, calls)
    posting = await ats_api.lookup(["https://example.com/x", "https://boards.greenhouse.io/acme/jobs/7"], client)
    assert posting is not None and posting.provider == "greenhouse"
    assert posting.title == "Data Engineer" and posting.company == "Acme" and posting.company_trusted
    assert "• Spark" in posting.description
    assert calls == [f"{base}/jobs/7"]  # the company name was in the posting: no second request


async def test_workday_lever_smartrecruiters_workable_and_ashby_postings():
    calls: list[str] = []
    routes = {
        "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/job/Remote/Data-Engineer_R1": {
            "jobPostingInfo": {"title": "Data Engineer", "jobDescription": JD_HTML},
            "hiringOrganization": {"name": "Acme Health"},
        },
        "https://api.lever.co/v0/postings/acme-corp/1b2c3d4e-0000-4000-8000-1234567890ab": {
            "text": "Platform Engineer",
            "descriptionPlain": "Build the platform. " * 10,
            "lists": [{"text": "Requirements", "content": "<li>Go</li><li>Kubernetes</li>"}],
            "additionalPlain": "We offer remote work.",
        },
        "https://api.smartrecruiters.com/v1/companies/Acme/postings/744000012345678": {
            "name": "Analyst",
            "company": {"name": "Acme Group"},
            "jobAd": {
                "sections": {
                    "jobDescription": {"title": "Job Description", "text": JD_HTML},
                    "qualifications": {"title": "Qualifications", "text": "<ul><li>SQL</li></ul>"},
                }
            },
        },
        "https://apply.workable.com/api/v2/accounts/acme/jobs/AB12": {
            "title": "Designer",
            "description": JD_HTML,
            "requirements": "<ul><li>Figma</li></ul>",
        },
        "https://apply.workable.com/api/v1/accounts/acme": {"name": "Acme Studio"},
        "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=false": {
            "jobs": [
                {"id": "other", "title": "Other", "descriptionPlain": "x" * 300},
                {"id": "1b2c3d4e-0000-4000-8000-1234567890ab", "title": "ML Engineer", "descriptionHtml": JD_HTML},
            ]
        },
    }
    client = _client(routes, calls)

    workday = await ats_api.lookup(
        ["https://acme.wd5.myworkdayjobs.com/en-US/External/job/Remote/Data-Engineer_R1"], client
    )
    assert workday and (workday.title, workday.company, workday.company_trusted) == (
        "Data Engineer",
        "Acme Health",
        True,
    )

    lever = await ats_api.lookup(["https://jobs.lever.co/acme-corp/1b2c3d4e-0000-4000-8000-1234567890ab"], client)
    assert lever and lever.title == "Platform Engineer"
    assert (lever.company, lever.company_trusted) == ("Acme Corp", False)
    assert "Requirements\n• Go\n• Kubernetes" in lever.description and "remote work" in lever.description

    smart = await ats_api.lookup(["https://jobs.smartrecruiters.com/Acme/744000012345678-analyst"], client)
    assert smart and smart.company == "Acme Group" and "Qualifications\n• SQL" in smart.description

    workable = await ats_api.lookup(["https://apply.workable.com/acme/j/AB12/"], client)
    assert workable and workable.company == "Acme Studio" and "Requirements\n• Figma" in workable.description

    ashby = await ats_api.lookup(["https://jobs.ashbyhq.com/acme/1b2c3d4e-0000-4000-8000-1234567890ab"], client)
    assert ashby and ashby.title == "ML Engineer" and ashby.company == "Acme" and not ashby.company_trusted


async def test_a_missing_or_empty_posting_is_no_answer():
    calls: list[str] = []
    client = _client(
        {"https://boards-api.greenhouse.io/v1/boards/acme/jobs/8": {"title": "X", "content": "<p>too short</p>"}},
        calls,
    )
    assert await ats_api.lookup(["https://boards.greenhouse.io/acme/jobs/9"], client) is None
    assert await ats_api.lookup(["https://boards.greenhouse.io/acme/jobs/8"], client) is None
    assert await ats_api.lookup(["https://jobright.ai/jobs/info/1"], client) is None


async def test_a_network_failure_is_no_answer():
    def broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    client = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    assert await ats_api.lookup(["https://boards.greenhouse.io/acme/jobs/7"], client) is None


# ---------------------------------------------------------------------------
# The page's own data (in Qt WebEngine)
# ---------------------------------------------------------------------------

NEXT_DATA_PAGE = """<!doctype html><html><head><title>Data Engineer | JobRight</title>
<script id="__NEXT_DATA__" type="application/json">{json}</script></head>
<body><main><h1>Data Engineer</h1><div class="card">Acme · Austin, TX · Full-time</div></main></body></html>"""


async def test_next_data_job_with_sections_becomes_a_candidate_and_similar_jobs_do_not_win(web_page):
    data = {
        "props": {
            "pageProps": {
                "jobResult": {
                    "jobTitle": "Data Engineer",
                    "jobSummary": "Build reliable data pipelines for analytics and product teams across the company.",
                    "coreResponsibilities": ["Design batch and streaming pipelines", "Own data quality and SLAs"],
                    "qualifications": {
                        "mustHave": ["5+ years of Python", "Strong SQL"],
                        "preferredHave": ["Spark", "Airflow"],
                    },
                },
                "companyResult": {"companyName": "Acme Health"},
                "similarJobs": [
                    {
                        "jobTitle": "Product Designer",
                        "companyName": "Globex",
                        "jobSummary": "Design delightful products for millions of users across web and mobile apps. "
                        * 3,
                    }
                ],
            }
        }
    }
    html = NEXT_DATA_PAGE.replace("{json}", json.dumps(data))
    assert await load_html(web_page, html, base_url="https://jobright.ai/jobs/info/abc")
    result = await extract_page(web_page)
    by_title = {candidate.title: candidate for candidate in result.candidates}
    main = by_title["Data Engineer"]
    assert main.source == "state" and main.company == "Acme Health" and main.sections >= 2
    assert "Responsibilities\n• Design batch and streaming pipelines" in main.description
    assert "Required\n• 5+ years of Python" in main.description and "Preferred\n• Spark" in main.description

    fused = fuse([Evidence("listing", role="Data Engineer", company="Acme Health"), *page_evidence(result)])
    assert fused.role.value == "Data Engineer" and fused.company.value == "Acme Health"
    assert "Strong SQL" in fused.jd.value and "Globex" not in fused.jd.value


async def test_react_server_component_chunks_are_read(web_page):
    job = {
        "job": {
            "title": "Backend Engineer",
            "company": {"name": "Initech"},
            "descriptionHtml": "<p>" + "Build APIs in Go and Postgres for our payments platform. " * 6 + "</p>",
        }
    }
    chunk = "1:" + json.dumps(job) + "\n"
    html = (
        "<!doctype html><html><body><h1>Backend Engineer</h1>"
        f"<script>self.__next_f = self.__next_f || []; self.__next_f.push([1, {json.dumps(chunk)}]);</script>"
        "</body></html>"
    )
    assert await load_html(web_page, html, base_url="https://careers.initech.test/jobs/42")
    result = await extract_page(web_page)
    assert [(c.title, c.company) for c in result.candidates] == [("Backend Engineer", "Initech")]
    assert "payments platform" in result.candidates[0].description


async def test_ats_addresses_on_the_page_are_collected(web_page):
    html = (
        "<!doctype html><html><body><h1>Data Engineer</h1>"
        '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>'
        '<a href="https://jobs.lever.co/acme/1b2c3d4e-0000-4000-8000-1234567890ab">Apply</a>'
        "</body></html>"
    )
    assert await load_html(web_page, html, base_url="https://acme.test/careers/position?gh_jid=4012345")
    result = await extract_page(web_page)
    assert "https://boards.greenhouse.io/acme/jobs/4012345" in result.ats_urls
    assert "https://jobs.lever.co/acme/1b2c3d4e-0000-4000-8000-1234567890ab" in result.ats_urls


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/job":
            body = json.dumps(
                {
                    "data": {
                        "jobTitle": "Site Reliability Engineer",
                        "companyName": "Hooli",
                        "jobDescription": "<p>"
                        + "Keep our services fast and available around the clock. " * 6
                        + "</p>",
                        "requirements": ["Linux", "Kubernetes", "On-call experience"],
                    }
                }
            ).encode()
            kind = "application/json"
        elif self.path == "/api/xhr":
            body = json.dumps({"ping": "pong", "items": list(range(30))}).encode()
            kind = "application/json"
        else:
            body = (
                "<!doctype html><html><body><h1>Loading…</h1><script>"
                "fetch('/api/job').then(r => r.json()).then(d => { document.querySelector('h1').textContent = d.data.jobTitle; });"
                "const x = new XMLHttpRequest(); x.open('GET', '/api/xhr'); x.send();"
                "</script></body></html>"
            ).encode()
            kind = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def job_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def _load(page: QWebEnginePage, url: str) -> None:
    loop = asyncio.get_running_loop()
    done: asyncio.Future[bool] = loop.create_future()
    page.loadFinished.connect(lambda ok: done.done() or done.set_result(ok))
    page.load(QUrl(url))
    assert await asyncio.wait_for(done, 30)


async def test_json_the_page_fetched_is_captured_and_read(web_profile, web_page, job_server):
    install_network_capture(web_profile)
    install_network_capture(web_profile)  # idempotent
    try:
        assert len(web_profile.scripts().find(NETWORK_CAPTURE_NAME)) == 1
        await _load(web_page, f"{job_server}/job.html")
        for _ in range(50):
            count = await run_js(web_page, "window.__RAI_NET__ ? window.__RAI_NET__.entries.length : -1")
            if count == 2:
                break
            await asyncio.sleep(0.1)
        urls = await run_js_json(web_page, "window.__RAI_NET__.entries.map(e => e.url)")
        assert sorted(url.rsplit("/", 1)[-1] for url in urls) == ["job", "xhr"]
        # The page itself still got its data.
        assert await run_js(web_page, "document.querySelector('h1').textContent") == "Site Reliability Engineer"

        result = await extract_page(web_page)
        net = [candidate for candidate in result.candidates if candidate.source == "net"]
        assert [(c.title, c.company) for c in net] == [("Site Reliability Engineer", "Hooli")]
        assert "Requirements\n• Linux" in net[0].description
    finally:
        for script in web_profile.scripts().find(NETWORK_CAPTURE_NAME):
            web_profile.scripts().remove(script)


async def test_reading_waits_for_the_page_to_stop_changing(web_page, monkeypatch):
    # An offscreen test page has its timers throttled to about one a second (the app's own
    # browser turns that off), so the quiet window here is longer than that.
    monkeypatch.setattr(extractor, "STABLE_MAX_SECONDS", 10.0)
    monkeypatch.setattr(extractor, "STABLE_QUIET_SECONDS", 1.6)
    html = (
        "<!doctype html><html><body><main id='m'><h1>Data Engineer</h1></main><script>"
        "let n = 0; const t = setInterval(() => { n += 1;"
        " document.getElementById('m').insertAdjacentHTML('beforeend', '<p>Build pipelines with Python and SQL for analytics, item ' + n + '.</p>');"
        " if (n >= 3) clearInterval(t); }, 80);"
        "</script></body></html>"
    )
    assert await load_html(web_page, html, base_url="https://acme.test/jobs/1")
    await ensure_script(web_page, extractor.MARKER, extractor._script())
    await extractor.wait_stable(web_page)
    paragraphs = await run_js(web_page, "document.querySelectorAll('#m p').length")
    assert paragraphs == 3


async def test_network_capture_stays_off_jobright_and_chatgpt(web_profile, web_page):
    install_network_capture(web_profile)
    try:
        for base, expected in (
            ("https://jobright.ai/jobs/info/1", False),
            ("https://chatgpt.com/", False),
            ("https://careers.acme.test/jobs/1", True),
        ):
            assert await load_html(web_page, "<!doctype html><html><body><h1>x</h1></body></html>", base_url=base)
            assert await run_js(web_page, "typeof window.__RAI_NET__ === 'object'") is expected, base
    finally:
        for script in web_profile.scripts().find(NETWORK_CAPTURE_NAME):
            web_profile.scripts().remove(script)
