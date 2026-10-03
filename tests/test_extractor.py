from __future__ import annotations

import pytest

from app.automation import extractor
from app.automation.extractor import Extraction, click_apply_control, extract_page, find_apply_control, tidy_jd
from app.automation.job_rules import decide_capture
from app.browser.jsbridge import call_async_js, ensure_script, run_js, run_js_json
from tests.helpers import FIXTURES, load_fixture, load_html

API = "window.__RAI_EXTRACT__"

EXTRACT_KEYS = {
    "url",
    "title",
    "role",
    "company",
    "jd_text",
    "jd_html_len",
    "form_field_count",
    "has_jobposting_ldjson",
    "apply_links",
    "is_linkedin",
    "linkedin_source",
    "prose_len",
    "source",
}


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    # The settle wait is for live single-page sites; fixtures are static.
    monkeypatch.setattr(extractor, "SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(extractor, "EXPAND_SECONDS", 0.05)


async def load_page(page, name: str, base_url: str) -> None:
    """Load a fixture as if it had been served from `base_url` (adapters go by host)."""
    html = (FIXTURES / "pages" / name).read_text(encoding="utf-8")
    assert await load_html(page, html, base_url=base_url)


async def inject(page) -> None:
    await ensure_script(page, API, extractor._script())


async def flag(page, name: str):
    return await run_js_json(page, f"window.{name} === undefined ? null : window.{name}")


# --- the script's contract -------------------------------------------------


async def test_extract_returns_exactly_the_documented_keys(web_page):
    await load_page(web_page, "greenhouse_job.html", "https://boards.greenhouse.io/acme/jobs/4012345")
    await inject(web_page)
    raw = await run_js_json(web_page, f"{API}.extract()")
    assert set(raw) == EXTRACT_KEYS
    verdict = await run_js_json(web_page, f"{API}.classify()")
    assert verdict == {"blocked": False, "reason": None, "detail": ""}
    # classify() accepts the extraction it should judge.
    assert await run_js_json(web_page, f"{API}.classify({API}.extract())") == verdict


async def test_script_functions_never_throw_on_a_bare_document(web_page):
    await load_html(web_page, "", base_url="https://example.test/")
    await inject(web_page)
    raw = await run_js_json(web_page, f"{API}.extract()")
    assert set(raw) == EXTRACT_KEYS
    assert raw["jd_text"] == "" and raw["apply_links"] == []
    assert await run_js_json(web_page, f"{API}.classify()") == {
        "blocked": True,
        "reason": "empty",
        "detail": "no role and no job description were found",
    }
    assert await run_js_json(web_page, f"{API}.expandDescription()") == 0
    assert await run_js_json(web_page, f"{API}.findApplyControl()") is None
    assert await run_js_json(web_page, f"{API}.clickApplyControl()") is False


# --- reading jobs ----------------------------------------------------------


async def test_jobposting_ldjson_page(web_page):
    await load_page(web_page, "ldjson_job.html", "https://northwind.example/careers/senior-platform-engineer")
    result = await extract_page(web_page)

    assert result.source == "ldjson"
    assert result.has_jobposting_ldjson is True
    assert result.role == "Senior Platform Engineer"
    assert result.company == "Northwind Robotics"
    assert "Kubernetes platform that schedules robot workloads" in result.jd_text
    assert "<li>" not in result.jd_text and "<p>" not in result.jd_text
    # Markup became line breaks, not one run-on line.
    assert "Responsibilities\n" in result.jd_text
    assert result.has_jd and not result.form_only
    assert result.url == "https://northwind.example/careers/senior-platform-engineer"
    assert result.title == "Careers | Northwind Robotics"
    # A full-screen consent prompt covers the page, but the posting is data: not blocked, nothing clicked.
    assert (result.blocked, result.block_reason) == (False, None)
    assert "privacy" not in result.jd_text.lower()
    assert await flag(web_page, "__consentClicked") is None


async def test_ldjson_double_encoded_description_and_string_organisation(web_page):
    html = """<title>x</title>
    <script type="application/ld+json">not json at all {</script>
    <script type="application/ld+json">[{"@type": "BreadcrumbList"}, {"@type": "JobPosting", "title": "QA Lead",
      "hiringOrganization": "Tyrell Corp",
      "description": "&lt;p&gt;Lead the quality group for our replicant firmware.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Plan
      releases with the hardware team and own the sign-off.&lt;/li&gt;&lt;li&gt;Grow three testers into automation
      engineers over the next year.&lt;/li&gt;&lt;/ul&gt;&lt;p&gt;Requirements: six years of testing embedded
      systems and experience with Python.&lt;/p&gt;"}]</script>
    <body><p>shell</p></body>"""
    await load_html(web_page, html, base_url="https://tyrell.example/jobs/9")
    result = await extract_page(web_page, settle=False)
    assert (result.role, result.company, result.source) == ("QA Lead", "Tyrell Corp", "ldjson")
    assert "&lt;" not in result.jd_text and "<li>" not in result.jd_text
    assert "Grow three testers into automation" in result.jd_text
    assert not result.blocked


async def test_greenhouse_page(web_page):
    await load_page(web_page, "greenhouse_job.html", "https://boards.greenhouse.io/acme/jobs/4012345?gh_src=x")
    result = await extract_page(web_page)

    assert result.source == "adapter:greenhouse"
    assert result.role == "Staff Data Engineer"
    assert result.company == "Acme Analytics"  # not "at Acme Analytics", and never "Greenhouse"
    assert "Own the ingestion platform built on Spark" in result.jd_text
    assert result.has_jobposting_ldjson is False
    assert result.jd_html_len > len(result.jd_text)
    # Form text, "similar jobs" and the footer are not part of the description.
    assert "First Name" not in result.jd_text
    assert "Analytics Engineer" not in result.jd_text
    assert "Powered by Greenhouse" not in result.jd_text
    # The posting has its own application form, yet it is a job page, not a form-only page.
    assert result.form_field_count == 5
    assert result.prose_len >= 600
    assert not result.form_only and result.has_jd and not result.blocked
    assert result.is_linkedin is False and result.linkedin_source is False
    # Reading never submits anything.
    assert await flag(web_page, "__submitted") is None


async def test_form_only_page(web_page):
    assert await load_fixture(web_page, "pages/form_only.html")
    result = await extract_page(web_page)

    # text, text, email, tel, file, textarea, select and ONE radio group; the hidden input does not count.
    assert result.form_field_count == 8
    assert result.prose_len < 600
    assert result.has_jobposting_ldjson is False
    assert result.form_only is True
    assert result.has_jd is False
    assert result.role == "Product Designer"
    # A form is not a page that needs a person.
    assert (result.blocked, result.block_reason) == (False, None)
    assert await flag(web_page, "__submitted") is None


async def test_linkedin_page(web_page):
    html = (FIXTURES / "pages" / "linkedin_job.html").read_text(encoding="utf-8")
    assert await load_html(web_page, html, base_url="https://www.linkedin.com/jobs/view/123")
    result = await extract_page(web_page)

    assert result.is_linkedin is True
    assert result.source == "adapter:linkedin"
    assert result.role == "Backend Engineer"
    assert result.company == "Globex"  # the employer, never "LinkedIn"
    assert "payments group" in result.jd_text


async def test_hiring_cafe_page(web_page):
    await load_page(web_page, "hiring_cafe_job.html", "https://hiring.cafe/viewjob/abc123")
    result = await extract_page(web_page)

    assert result.source == "adapter:hiring_cafe"
    assert result.role == "Security Analyst"
    assert result.company == "Soylent Corp"  # from "Role at Company | HiringCafe", never the board
    assert "Triage alerts from our SIEM" in result.jd_text
    assert result.apply_links == ["https://careers.soylent.example/jobs/881?utm_source=hiringcafe"]
    assert result.linkedin_source is False and not result.blocked and result.has_jd


async def test_job_description_in_a_same_origin_iframe(web_page):
    await load_page(web_page, "iframe_job.html", "https://piedpiper.example/careers/compression-engineer")
    result = await extract_page(web_page)

    assert "FRAME-ONLY marker" in result.jd_text
    assert result.role == "Compression Engineer"
    assert result.has_jd and not result.blocked


async def test_generic_page_strips_chrome_and_similar_jobs(web_page):
    html = """<title>Data Analyst - Dunder Mifflin</title>
    <body>
      <nav>NAVIGATION-TEXT <a href="/a">Products</a></nav>
      <div class="page">
        <h1>Data Analyst</h1>
        <div class="posting">
          <p>Dunder Mifflin sells paper, and the analytics team tells the sales floor where to sell it next.</p>
          <p>Responsibilities: build weekly reports, keep the warehouse models tidy, answer questions from branches.</p>
          <p>Requirements: two years of SQL, experience with a BI tool, and comfort presenting to salespeople.</p>
          <span style="display:none">INVISIBLE-TEXT that only scripts can see and that is long enough to matter.</span>
          <h2>Similar jobs</h2>
          <ul><li>SIMILAR-JOB-TEXT Sales Associate, requirements: a licence and experience with people.</li></ul>
        </div>
      </div>
      <footer>FOOTER-TEXT about benefits and requirements of using this site.</footer>
    </body>"""
    await load_html(web_page, html, base_url="https://dundermifflin.example/openings/42")
    result = await extract_page(web_page, settle=False)

    assert result.source == "generic"
    assert (result.role, result.company) == ("Data Analyst", "Dunder Mifflin")
    assert "analytics team tells the sales floor" in result.jd_text
    for chrome in ("NAVIGATION-TEXT", "INVISIBLE-TEXT", "SIMILAR-JOB-TEXT", "FOOTER-TEXT"):
        assert chrome not in result.jd_text
    assert not result.blocked


# --- jobright detail page and the Apply control ------------------------------


async def test_jobright_detail_page_and_apply_control(web_page):
    await load_page(web_page, "jobright_detail.html", "https://jobright.ai/jobs/info/66f0a1b2c3d4e5f6a7b8c9d0")
    result = await extract_page(web_page)

    assert result.source == "adapter:jobright"
    assert result.role == "Machine Learning Engineer"
    assert result.company == "Initech"  # never "Jobright"
    assert "Train and ship ranking models" in result.jd_text
    assert "Three or more years of applied machine learning" in result.jd_text
    assert "Data Scientist at Umbrella" not in result.jd_text  # similar jobs
    assert result.has_jd and not result.blocked
    # LinkedIn links to people and to the site's own profile are not the job's source.
    assert result.linkedin_source is False
    assert result.is_linkedin is False

    control = await find_apply_control(web_page)
    assert control == {"text": "Apply with Autofill", "href": None, "tag": "button"}
    # Finding, and reading the page, clicked nothing.
    assert await flag(web_page, "__applyClicked") is None

    assert await click_apply_control(web_page) is True
    assert await flag(web_page, "__applyClicked") == 1


async def test_jobright_page_whose_apply_link_goes_to_linkedin(web_page):
    await load_page(web_page, "jobright_linkedin.html", "https://jobright.ai/jobs/info/77aa")
    result = await extract_page(web_page)

    assert result.linkedin_source is True
    assert result.is_linkedin is False  # the page itself is jobright
    assert result.apply_links == ["https://www.linkedin.com/jobs/view/3999999999?refId=abc"]
    assert result.role == "Account Executive"

    control = await find_apply_control(web_page)
    assert control == {
        "text": "Apply Now",
        "href": "https://www.linkedin.com/jobs/view/3999999999?refId=abc",
        "tag": "a",
    }


async def test_linkedin_source_label_without_a_link(web_page):
    html = """<title>Nurse @ Mercy | Jobright.ai</title><main><h1>Nurse</h1>
      <div class="meta"><span>Posted 3 days ago</span> <span>Source: LinkedIn</span></div>
      <button>Apply Now</button></main>"""
    await load_html(web_page, html, base_url="https://jobright.ai/jobs/info/1")
    result = await extract_page(web_page, settle=False)
    assert result.linkedin_source is True

    prose = """<title>Nurse @ Mercy | Jobright.ai</title><main><h1>Nurse</h1>
      <p>You can import your profile from LinkedIn when you apply, or sign in with LinkedIn.</p></main>"""
    await load_html(web_page, prose, base_url="https://jobright.ai/jobs/info/2")
    result = await extract_page(web_page, settle=False)
    assert result.linkedin_source is False


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("<button>Save</button><button>Apply Now</button><button>APPLY WITH AUTOFILL</button>", "APPLY WITH AUTOFILL"),
        ("<a href='/x'>apply now</a><button>Apply</button>", "apply now"),
        ("<button>Applied</button><button>Apply</button>", "Apply"),
        ("<button>Apply with LinkedIn</button><button>Submit application</button>", None),
        ("<button>Submit and Apply Now</button>", None),
        ("<button style='display:none'>Apply Now</button>", None),
        ("<button disabled>Apply Now</button>", None),
        ("<input type='submit' value='Apply Now'>", None),
    ],
)
async def test_apply_control_matching(web_page, body, expected):
    await load_html(web_page, f"<title>t</title><main>{body}</main>", base_url="https://jobright.ai/jobs/info/1")
    control = await find_apply_control(web_page)
    assert (control or {}).get("text") == expected
    if expected is None:
        assert await click_apply_control(web_page) is False


async def test_apply_control_never_clicks_submit(web_page):
    html = """<title>t</title><main>
      <button onclick="window.__submitted = true">Submit application</button>
      <button onclick="window.__submitted = true">Apply now and submit</button>
      <button onclick="window.__applied = true">Apply Now</button></main>"""
    await load_html(web_page, html, base_url="https://jobright.ai/jobs/info/1")
    assert await click_apply_control(web_page) is True
    assert await flag(web_page, "__applied") is True
    assert await flag(web_page, "__submitted") is None


# --- blocked pages ---------------------------------------------------------


async def test_login_wall_is_blocked(web_page):
    await load_page(web_page, "blocked_login.html", "https://talenthub.example/jobs/55")
    result = await extract_page(web_page)
    assert (result.blocked, result.block_reason) == (True, "login")
    assert result.block_detail
    assert not result.has_jd
    assert await flag(web_page, "__loginClicked") is None


async def test_auth_page_without_a_password_field_is_blocked(web_page):
    html = """<title>Welcome</title><main><h1>Welcome</h1>
      <a href="/oauth/google">Continue with Google</a><a href="/oauth/apple">Continue with Apple</a></main>"""
    await load_html(web_page, html, base_url="https://id.talenthub.example/login?next=%2Fjobs%2F55")
    result = await extract_page(web_page, settle=False)
    assert (result.blocked, result.block_reason) == (True, "login")


async def test_full_screen_consent_wall_is_blocked(web_page):
    await load_page(web_page, "blocked_consent.html", "https://contoso.example/careers/jobs/77")
    result = await extract_page(web_page)
    assert (result.blocked, result.block_reason) == (True, "consent")
    # The consent text is not mistaken for a job description, and nothing is accepted for the user.
    assert result.jd_text == ""
    assert await flag(web_page, "__consentClicked") is None


@pytest.mark.parametrize("title", ["jobs.example.com", "Just a moment..."])
async def test_verify_you_are_human_page_is_blocked(web_page, title):
    html = (FIXTURES / "pages" / "blocked_captcha.html").read_text(encoding="utf-8")
    html = html.replace("<title>jobs.example.com</title>", f"<title>{title}</title>")
    assert await load_html(web_page, html, base_url="https://jobs.example.com/positions/12")
    result = await extract_page(web_page)
    assert (result.blocked, result.block_reason) == (True, "captcha")
    assert await flag(web_page, "__captchaClicked") is None


@pytest.mark.parametrize(
    "text",
    [
        "Our systems have detected unusual traffic from your computer network.",
        "Access denied. You don't have permission to access this page on this server.",
    ],
)
async def test_other_security_pages_are_captcha(web_page, text):
    await load_html(web_page, f"<title>example.com</title><body><h1>Hold on</h1><p>{text}</p></body>")
    result = await extract_page(web_page, settle=False)
    assert (result.blocked, result.block_reason) == (True, "captcha")


async def test_job_no_longer_available_page_is_blocked(web_page):
    await load_page(web_page, "blocked_404.html", "https://vandelay.example/careers/jobs/404-importer")
    result = await extract_page(web_page)
    assert (result.blocked, result.block_reason) == (True, "http_error")


@pytest.mark.parametrize(
    "html",
    [
        "<title>404 Not Found</title><body><h1>Not Found</h1><p>The requested URL was not found.</p></body>",
        "<title>Acme</title><body><h1>Error 503</h1><p>Service unavailable.</p></body>",
        "<title>Acme careers</title><body><h1>Oops</h1><p>The page you are looking for does not exist.</p></body>",
    ],
)
async def test_http_error_pages_are_blocked(web_page, html):
    await load_html(web_page, html, base_url="https://acme.example/jobs/1")
    result = await extract_page(web_page, settle=False)
    assert (result.blocked, result.block_reason) == (True, "http_error")


async def test_page_with_nothing_to_read_is_empty(web_page):
    html = "<title>Careers at Acme</title><body><div id='root'></div></body>"
    await load_html(web_page, html, base_url="https://acme.example/jobs/1")
    result = await extract_page(web_page)
    assert (result.blocked, result.block_reason) == (True, "empty")
    assert not result.form_only


async def test_overlay_cookie_banner_does_not_block_and_is_not_clicked(web_page):
    await load_page(web_page, "cookie_banner_overlay.html", "https://stark.example/careers/sre")
    result = await extract_page(web_page)

    assert (result.blocked, result.block_reason, result.block_detail) == (False, None, "")
    assert result.role == "Site Reliability Engineer"
    assert result.company == "Stark Logistics"
    assert "Define service level objectives" in result.jd_text
    assert "cookie" not in result.jd_text.lower()
    assert result.has_jd
    assert await flag(web_page, "__consentClicked") is None


# --- the harmless fixes ------------------------------------------------------


async def test_show_more_inside_the_description_is_expanded(web_page):
    await load_page(web_page, "show_more.html", "https://wonka.example/careers/technical-writer")

    before = await extract_page(web_page, settle=False)
    assert "HIDDEN-UNTIL-EXPANDED" not in before.jd_text
    assert await flag(web_page, "__moreClicks") is None

    result = await extract_page(web_page)
    assert "HIDDEN-UNTIL-EXPANDED" in result.jd_text
    assert "docs-as-code" in result.jd_text
    assert "Show less" not in result.jd_text
    assert await flag(web_page, "__moreClicks") == 1
    # Only the description's own toggle: not Apply, not "Show more jobs", not the cookie banner's buttons.
    assert await flag(web_page, "__applyClicked") is None
    assert await flag(web_page, "__otherClicked") is None
    assert await flag(web_page, "__consentClicked") is None

    # Already expanded: a second read does not toggle it shut again.
    again = await extract_page(web_page)
    assert "HIDDEN-UNTIL-EXPANDED" in again.jd_text
    assert await flag(web_page, "__moreClicks") == 1


async def test_expand_never_clicks_links_or_gates(web_page):
    html = """<title>t</title><main><div class="job-description"><p>Short teaser.</p>
      <a href="/full" onclick="window.__nav = true; return false;">Read more</a>
      <button onclick="window.__gate = true">Accept and show more</button>
      <button onclick="window.__gate = true">Sign in to see more</button>
      <form><button type="button" onclick="window.__gate = true">Show more</button></form>
      </div></main>"""
    await load_html(web_page, html)
    await inject(web_page)
    assert await run_js_json(web_page, f"{API}.expandDescription()") == 0
    assert await flag(web_page, "__nav") is None
    assert await flag(web_page, "__gate") is None


async def test_late_rendered_description_is_read_after_the_settle_wait(web_page, monkeypatch):
    # Offscreen pages throttle their timers to about one a second.
    monkeypatch.setattr(extractor, "SETTLE_SECONDS", 2.5)
    html = """<title>Careers</title><body><div id="root"></div><script>
      setTimeout(() => { document.getElementById('root').innerHTML =
        '<h1>Fleet Manager</h1><div class="job-description"><p>Oceanic Airlines needs a Fleet Manager to plan ' +
        'maintenance for forty aircraft.</p><p>Responsibilities: schedule checks, manage vendors and report to ' +
        'the regulator.</p><p>Requirements: ten years in aviation maintenance and experience with planning ' +
        'software.</p></div>'; }, 400);
    </script></body>"""
    await load_html(web_page, html, base_url="https://oceanic.example/jobs/815")
    first = await extract_page(web_page, settle=False)
    assert (first.blocked, first.block_reason) == (True, "empty")

    await load_html(web_page, html, base_url="https://oceanic.example/jobs/815")
    result = await extract_page(web_page)
    assert not result.blocked and result.has_jd
    assert result.role == "Fleet Manager"


async def test_scroll_for_lazy_resolves(web_page):
    await load_html(web_page, "<title>t</title><div style='height:5000px'>tall</div>")
    await inject(web_page)
    assert await call_async_js(web_page, f"{API}.scrollForLazy()", timeout=20) is True


async def test_scroll_for_lazy_steps_through_a_visible_page_and_comes_back(web_page):
    await load_html(web_page, "<title>t</title><div style='height:5000px'>tall</div>")
    await inject(web_page)
    await run_js(web_page, "Object.defineProperty(document, 'visibilityState', { get: () => 'visible' }); true")
    assert await call_async_js(web_page, f"{API}.scrollForLazy()", timeout=30) is True
    assert await run_js_json(web_page, "document.scrollingElement.scrollTop") == 0


# --- more sites and awkward markup -------------------------------------------

JD_HTML = """<p>We are hiring an engineer to look after the systems our customers depend on every day.</p>
<p>Responsibilities: design services, review code, and take part in the on-call rotation with the team.</p>
<p>Requirements: five years of experience with distributed systems and clear written communication.</p>"""


async def test_lever_page_takes_the_company_from_the_logo(web_page):
    html = f"""<title>Umbrella - Infrastructure Engineer</title>
      <div class="main-header"><a class="main-header-logo" href="/"><img alt="Umbrella logo" src="data:,"></a></div>
      <div class="content"><div class="posting-page">
        <div class="posting-headline"><h2>Infrastructure Engineer</h2></div>
        <div class="section page-centered">{JD_HTML}</div>
        <a class="postings-btn" href="/umbrella/22053072-4c22-49c4-8299-28e107ceeb98/apply">Apply for this job</a>
      </div></div>"""
    await load_html(web_page, html, base_url="https://jobs.lever.co/umbrella/22053072-4c22-49c4-8299-28e107ceeb98")
    result = await extract_page(web_page, settle=False)
    assert result.source == "adapter:lever"
    assert (result.role, result.company) == ("Infrastructure Engineer", "Umbrella")
    assert result.apply_links == ["https://jobs.lever.co/umbrella/22053072-4c22-49c4-8299-28e107ceeb98/apply"]
    assert result.has_jd and not result.blocked


async def test_workday_page_takes_the_company_from_the_host(web_page):
    html = f"""<title>Workday</title><div id="root">
      <h2 data-automation-id="jobPostingHeader">Platform Engineer</h2>
      <div data-automation-id="jobPostingDescription">{JD_HTML}</div></div>"""
    url = "https://cyberdyne.wd5.myworkdayjobs.com/en-US/External/job/Austin/Platform-Engineer_R123"
    await load_html(web_page, html, base_url=url)
    result = await extract_page(web_page, settle=False)
    assert result.source == "adapter:workday"
    assert (result.role, result.company) == ("Platform Engineer", "Cyberdyne")
    assert result.has_jd and not result.blocked


async def test_state_classes_on_page_wrappers_do_not_hide_the_job(web_page):
    # A wrapper that merely carries a cookie-related state class, and an app root made inert by an open
    # dialog, are still the page.
    html = f"""<title>Role at Acme</title><body class="cookie-consent-open">
      <div id="app" class="has-cookie-banner" aria-hidden="true"><main><h1>Support Engineer</h1>
      <div class="job-description">{JD_HTML}</div></main></div>
      <div role="dialog" aria-label="Cookie consent" style="position:fixed;bottom:0;left:0;right:0;height:120px">
        <p>We use cookies.</p><button onclick="window.__consentClicked = true">Accept all</button></div></body>"""
    await load_html(web_page, html, base_url="https://acme.example/jobs/7")
    result = await extract_page(web_page)
    assert result.role == "Support Engineer"
    assert "on-call rotation" in result.jd_text and "cookies" not in result.jd_text
    assert not result.blocked and result.has_jd
    assert await flag(web_page, "__consentClicked") is None


async def test_page_with_a_captcha_widget_and_a_real_description_is_not_blocked(web_page):
    html = f"""<title>Role at Acme</title><main><h1>Support Engineer</h1><div class="job-description">{JD_HTML}</div>
      <form><input name="a"><input name="b"><div class="g-recaptcha"></div><button>Send</button></form></main>"""
    await load_html(web_page, html, base_url="https://acme.example/jobs/7")
    result = await extract_page(web_page, settle=False)
    assert not result.blocked and result.has_jd


async def test_sign_in_link_in_the_header_of_a_job_page_is_not_a_login_wall(web_page):
    html = f"""<title>Support Engineer at Acme</title><header><a href="/login">Sign in</a></header>
      <main><h1>Support Engineer</h1><div class="job-description">{JD_HTML}</div></main>"""
    await load_html(web_page, html, base_url="https://acme.example/jobs/7")
    result = await extract_page(web_page, settle=False)
    assert not result.blocked and result.has_jd


# --- the Python side ---------------------------------------------------------


async def test_a_page_that_cannot_answer_is_a_timeout_not_an_exception(web_page, monkeypatch):
    await load_html(web_page, "<title>t</title><p>x</p>", base_url="https://acme.example/jobs/1")

    async def broken(*_args, **_kwargs):
        raise extractor.JsError("The page changed while a script was running.")

    monkeypatch.setattr(extractor, "run_js_json", broken)
    result = await extract_page(web_page)
    assert (result.blocked, result.block_reason) == (True, "timeout")
    assert result.url == "https://acme.example/jobs/1"
    assert result.jd_text == "" and not result.has_jd
    assert await find_apply_control(web_page) is None
    assert await click_apply_control(web_page) is False


async def test_tampered_script_results_are_coerced(web_page):
    await load_html(web_page, "<title>t</title><p>x</p>", base_url="https://acme.example/jobs/1")
    tampered = """window.__RAI_EXTRACT__ = { read: () => ({
        extraction: { url: 5, title: ['x'], role: 'R'.repeat(999), company: null, jd_text: 'a\\n\\n\\n\\n\\nb',
          jd_html_len: 'many', form_field_count: -4, has_jobposting_ldjson: 'yes',
          apply_links: ['javascript:alert(1)', 'https://ok.example/apply', 7], is_linkedin: 1, linkedin_source: {},
          prose_len: 1e99, source: 'evil' },
        classification: { blocked: true, reason: 'made-up', detail: 9 } }) }; true"""
    await run_js(web_page, tampered)
    result = await extract_page(web_page, settle=False)
    assert result.url == "https://acme.example/jobs/1"
    assert result.title == "" and result.company == ""
    assert len(result.role) == 160
    assert result.jd_text == "a\n\nb"
    assert (result.jd_html_len, result.form_field_count) == (0, 0)
    assert result.has_jobposting_ldjson is False and result.is_linkedin is False and result.linkedin_source is False
    assert result.apply_links == ["https://ok.example/apply"]
    assert result.source == "generic"
    assert (result.blocked, result.block_reason, result.block_detail) == (True, "empty", "")


def test_tidy_jd_caps_length_and_blank_lines():
    assert tidy_jd("a\r\n\r\n\r\n\r\nb  \n\n\n\nc") == "a\n\nb\n\nc"
    assert len(tidy_jd("x" * 60_000)) == 50_000


def test_extraction_properties():
    jd = "x" * 100
    assert Extraction(jd_text=jd, prose_len=900, form_field_count=6).has_jd
    assert not Extraction(jd_text="x" * 99, prose_len=900).has_jd
    form = Extraction(jd_text=jd, prose_len=599, form_field_count=3)
    assert form.form_only and not form.has_jd
    assert Extraction(jd_text=jd, prose_len=10, form_field_count=9, has_jobposting_ldjson=True).has_jd


async def test_extraction_feeds_the_capture_rules(web_page):
    await load_page(web_page, "jobright_detail.html", "https://jobright.ai/jobs/info/66f0a1b2c3d4e5f6a7b8c9d0")
    saved = decide_capture(await extract_page(web_page), "jobright")
    assert (saved.save, saved.code, saved.toast) == (True, "saved", "Saved: Machine Learning Engineer at Initech")
    assert saved.jd_text is not None and "Train and ship ranking models" in saved.jd_text

    await load_page(web_page, "jobright_linkedin.html", "https://jobright.ai/jobs/info/77aa")
    assert decide_capture(await extract_page(web_page), "jobright").toast == "Skipped: LinkedIn job"

    assert await load_fixture(web_page, "pages/form_only.html")
    assert decide_capture(await extract_page(web_page), "hiring_cafe").toast == "Skipped: no job description"

    await load_page(web_page, "blocked_login.html", "https://talenthub.example/jobs/55")
    assert decide_capture(await extract_page(web_page), "hiring_cafe").toast == "Not saved: page needs sign-in"

    await load_page(web_page, "greenhouse_job.html", "https://boards.greenhouse.io/acme/jobs/4012345")
    assert (
        decide_capture(await extract_page(web_page), "hiring_cafe").toast
        == "Saved: Staff Data Engineer at Acme Analytics"
    )
