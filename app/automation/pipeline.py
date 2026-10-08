"""The generation run: saved jobs → ChatGPT → validated resume, one job at a time.

The pipeline owns the order of steps and every decision about a job. The
screen is behind the `PipelineHost` interface: the pipeline asks it to show a
review panel, a countdown or an alert and awaits the answer, so the same code
runs in the app and, with a scripted host, in tests.

What it never does: type a password, pass a CAPTCHA, click through a consent
or login wall, or press Submit on an application. A page that needs a person
is handed to the person.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar

from app.automation import chatgpt
from app.automation.chat_url import chat_id_from_url
from app.automation.extractor import Extraction, click_apply_control, extract_page
from app.automation.job_rules import BLOCK_MESSAGES, MIN_JD_CHARS, site_of, url_key
from app.automation.prompt import compose_prompt, compose_retry_message
from app.browser.browser import BrowserTab, BrowserWidget
from app.data.models import SavedJob
from app.services.jobcheck import JobCheck, check_job
from app.sync.api_client import ApiError
from app.ui.context import AppContext

log = logging.getLogger(__name__)

T = TypeVar("T")
Mode = Literal["automation", "human"]
CHATGPT_URL = "https://chatgpt.com/"
MAX_JSON_ATTEMPTS = 3
#: After this many jobs in a row that ChatGPT did not answer, the run pauses and asks,
#: instead of working through (and emptying) the whole queue during an outage.
CHATGPT_FAILURES_BEFORE_PAUSE = 3
#: Only web addresses are stored as an apply URL (the web app refuses anything else).
WEB_SCHEMES: tuple[str, ...] = ("http://", "https://")


class Stopped(Exception):  # noqa: N818
    """The user pressed Stop."""


class SkipJob(Exception):  # noqa: N818
    """This job ends without a resume; the run goes on."""

    def __init__(self, reason: str, detail: str = "", keep_saved: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.keep_saved = keep_saved


@dataclass
class ReviewData:
    company: str
    role: str
    jd_text: str
    jd_url: str
    apply_url: str | None
    check: JobCheck | None = None
    #: The page looked like a bare application form; the AI check must confirm the description.
    form_only: bool = False


@dataclass
class SkipRecord:
    role: str
    company: str
    reason: str
    detail: str = ""


@dataclass
class RunSummary:
    generated: int = 0
    skipped: list[SkipRecord] = field(default_factory=list)
    #: Jobs set aside because their page needed the user; they stay in Saved Jobs.
    attention: list[SkipRecord] = field(default_factory=list)
    stopped: bool = False
    last_resume_id: str | None = None

    @property
    def processed(self) -> int:
        return self.generated + len(self.skipped) + len(self.attention)


SKIP_LABELS: dict[str, str] = {
    "linkedin": "LinkedIn job",
    "form_only_no_jd": "Application form with no job description",
    "user_cancelled": "Cancelled by you",
    "needs_user_action": "Needs your attention",
    "extraction_failed": "Job details could not be read",
    "chatgpt_failed": "ChatGPT did not answer",
    "invalid_json": "ChatGPT's reply was not a valid resume",
}


def validation_problem(data: ReviewData, limits: dict[str, int]) -> str | None:
    """Why this job cannot go to ChatGPT yet, in plain words; None when it can."""
    if not data.company.strip():
        return "Company is empty."
    if not data.role.strip():
        return "Role is empty."
    if len(data.company.strip()) > limits["companyMax"]:
        return f"Company must be {limits['companyMax']} characters or fewer."
    if len(data.role.strip()) > limits["roleMax"]:
        return f"Role must be {limits['roleMax']} characters or fewer."
    length = len(data.jd_text.strip())
    if length == 0:
        return "Job description is empty."
    if length < limits["jobDescriptionMin"]:
        return f"Job description is too short: {length} of at least {limits['jobDescriptionMin']} characters."
    if length > limits["jobDescriptionMax"]:
        return f"Job description is too long: {length:,} of at most {limits['jobDescriptionMax']:,} characters."
    if len(data.jd_url) > limits["jobUrlMax"]:
        return "The job URL is too long to store."
    return None


class PipelineHost(Protocol):
    """What the pipeline needs from the screen."""

    browser: BrowserWidget

    def set_step(self, step: int, status: str) -> None: ...

    def log(self, text: str) -> None: ...

    def mark(self, job_id: str, state: str) -> None:
        """state: current | done | skipped | attention"""

    async def review(self, job: SavedJob, data: ReviewData, mode: Mode) -> ReviewData | None:
        """Show the review split. Returns the (possibly edited) data on Next, None on Cancel Job."""

    async def blocked(self, job: SavedJob, message: str, mode: Mode) -> str:
        """A job page needs the user. Returns retry | skip | cancel."""

    async def chatgpt_blocked(self, message: str) -> str:
        """ChatGPT needs the user (login, challenge). Returns retry | stop."""

    async def json_failed(self, attempt: int, issues: list[dict[str, Any]], raw: str, mode: Mode) -> str:
        """Validation failed. Returns retry | skip."""

    async def web_unreachable(self, message: str) -> str:
        """The web app could not validate. Returns retry | stop."""

    async def countdown(self, seconds: int, text: str) -> None:
        """The Automation delay before a step. Raises Stopped if Stop is pressed."""


class Pipeline:
    def __init__(
        self,
        ctx: AppContext,
        host: PipelineHost,
        mode: Mode,
        *,
        include_attention: bool = False,
        chatgpt_url: str = CHATGPT_URL,
    ) -> None:
        self.ctx = ctx
        self.host = host
        self.mode: Mode = mode
        self.include_attention = include_attention
        self.chatgpt_url = chatgpt_url
        self.cancel = asyncio.Event()
        self.summary = RunSummary()
        self._chat_failures = 0
        self.on_generated: Callable[[str], None] | None = None

    # -- control ---------------------------------------------------------------------

    def stop(self) -> None:
        """Abort at the next safe point. Nothing half-done is ever saved."""
        self.cancel.set()

    def _check(self) -> None:
        if self.cancel.is_set():
            raise Stopped()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.cancel.wait(), timeout=seconds)
        except TimeoutError:
            return
        raise Stopped()

    async def _race(self, awaitable: Awaitable[T]) -> T:
        """Await something, but give up at once when Stop is pressed."""
        task: asyncio.Future[T] = asyncio.ensure_future(awaitable)
        stop = asyncio.ensure_future(self.cancel.wait())
        try:
            await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        if not task.done():
            task.cancel()
            raise Stopped()
        return task.result()

    async def _step_delay(self) -> None:
        """Automation: the short countdown shown before every step transition."""
        if self.mode != "automation":
            return
        await self.host.countdown(self.ctx.settings.automation_seconds, "Continuing in {n} seconds…")
        self._check()

    def queue(self) -> list[SavedJob]:
        # Human Check picks jobs needing attention up again; Automation leaves them unless asked.
        # The list is read again before every job, so it is the light one (no job descriptions);
        # `run` loads the one job it is about to work on in full.
        return self.ctx.store.queue(include_attention=self.include_attention or self.mode == "human", light=True)

    # -- the run ---------------------------------------------------------------------

    async def run(self) -> RunSummary:
        store = self.ctx.store
        done: set[str] = set()
        try:
            while True:
                self._check()
                picked = next((item for item in self.queue() if item.id not in done), None)
                if picked is None:
                    break
                done.add(picked.id)
                job = store.get_saved_job(picked.id)
                if job is None or job.deleted_at is not None:
                    continue  # removed between the two reads
                self.host.mark(job.id, "current")
                record = SkipRecord(job.role or "Unknown role", job.company or "Unknown company", "")
                try:
                    try:
                        resume_id = await self._process(job)
                    except (SkipJob, Stopped, chatgpt.Cancelled):
                        raise
                    except Exception as error:  # noqa: BLE001 - one job's surprise must not end the run
                        # Type only: an exception message can quote page or resume text.
                        log.error("pipeline.job_failed %s", type(error).__name__)
                        raise SkipJob(
                            "extraction_failed", "an unexpected problem; the job is still saved", keep_saved=True
                        ) from error
                except SkipJob as skip:
                    record.reason, record.detail = skip.reason, skip.detail
                    store.skip_saved_job(job.id, reason=skip.reason, detail=skip.detail, keep_saved=skip.keep_saved)
                    if skip.keep_saved:
                        self.summary.attention.append(record)
                        self.host.mark(job.id, "attention")
                    else:
                        self.summary.skipped.append(record)
                        self.host.mark(job.id, "skipped")
                    self.host.log(f"Skipped: {SKIP_LABELS.get(skip.reason, skip.reason)}")
                    self.ctx.changed("jobs")
                    await self._count_chat_failure(skip.reason == "chatgpt_failed")
                else:
                    self._chat_failures = 0
                    self.summary.generated += 1
                    self.summary.last_resume_id = resume_id
                    self.host.mark(job.id, "done")
                    if self.on_generated is not None:
                        self.on_generated(resume_id)
                    self.ctx.changed("resumes")
                finally:
                    self._close_tabs()
                if self.mode == "automation" and any(item.id not in done for item in self.queue()):
                    await self._step_delay()  # before the next job
        except (Stopped, chatgpt.Cancelled):
            self.summary.stopped = True
            self.host.log("Stopped.")
        finally:
            self._close_tabs()
        return self.summary

    async def _count_chat_failure(self, failed: bool) -> None:
        if not failed:
            return
        self._chat_failures += 1
        if self._chat_failures < CHATGPT_FAILURES_BEFORE_PAUSE:
            return
        self._chat_failures = 0
        message = f"ChatGPT has not answered {CHATGPT_FAILURES_BEFORE_PAUSE} jobs in a row."
        self.host.log(f"Paused: {message}")
        if await self.host.chatgpt_blocked(message) != "retry":
            raise Stopped()

    def _close_tabs(self) -> None:
        # The generator's browser belongs to the run: every tab a job opened (pop-ups included) goes.
        try:
            self.host.browser.close_all()
        except RuntimeError:
            pass

    def _open(self, url: str) -> BrowserTab:
        return self.host.browser.add_tab(url)

    # -- one job ---------------------------------------------------------------------

    async def _process(self, job: SavedJob) -> str:
        host = self.host
        host.set_step(1, "Opening the job page…")
        host.log(f"Job: {site_of(job.url)} · opening")
        data = await self._resolve(job)
        self._check()

        host.set_step(1, "Checking role and company…")
        review_tab = self.host.browser.current()
        facts = _Facts(data, review_tab.title() if review_tab is not None else "")
        data.check = await self._race(check_job(self.ctx.api, facts))
        if data.form_only and (
            data.check.unchecked or not data.check.is_job_posting or not data.check.has_job_description
        ):
            # The page looked like a bare application form, and the AI check did not overrule that.
            raise SkipJob("form_only_no_jd")
        if data.check.role:
            data.role = data.check.role
        if data.check.company:
            data.company = data.check.company
        self._check()

        host.set_step(1, "Review the extracted information")
        reviewed = await host.review(job, data, self.mode)
        self._check()
        if reviewed is None:
            raise SkipJob("user_cancelled")
        problem = validation_problem(reviewed, self.ctx.limits)
        if problem is not None:
            # Human Check never gets here (the panel refuses Next); Automation sets the job aside.
            raise SkipJob("needs_user_action", problem.rstrip("."), keep_saved=True)

        reply, chat_tab = await self._generate(job, reviewed)
        resume, chat_tab = await self._validate(reply, chat_tab)

        host.set_step(3, "Saving…")
        chat_url = await self._conversation_url(chat_tab)
        self._check()  # Stop pressed during the last wait: nothing is saved
        name = str((resume.get("contact") or {}).get("fullName") or "").strip() or self.ctx.candidate_name
        saved = self.ctx.store.save_generated(
            saved_job_id=job.id,
            candidate_name=name[:120],
            role=reviewed.role.strip(),
            company=reviewed.company.strip(),
            jd_url=reviewed.jd_url,
            apply_url=reviewed.apply_url,
            source_url=job.url,
            url_key=job.url_key or url_key(job.url),
            jd_text=reviewed.jd_text.strip(),
            chat_url=chat_url,
            chat_id=chat_id_from_url(chat_url),
            resume_json=json.dumps(resume, ensure_ascii=False),
        )
        host.log("Saved.")
        return saved.id

    # -- step 1: the real job page -----------------------------------------------------

    async def _load(self, job: SavedJob, tab: BrowserTab) -> None:
        """Wait for a tab to load; a timeout is a blocked page like any other."""
        while not await self._race(tab.wait_loaded(self.ctx.config.page_load_timeout_seconds)):
            self._check()
            await self._blocked(job, BLOCK_MESSAGES["timeout"])
            tab.stop()
            await self._sleep(0.3)  # let the aborted load report itself before the new one starts
            tab.reload()

    async def _blocked(self, job: SavedJob, message: str) -> None:
        """Hand a blocked page to the user. Returns when they ask to retry; raises SkipJob otherwise."""
        self.host.log(f"Needs attention: {message}")
        action = await self.host.blocked(job, message, self.mode)
        self._check()
        if action == "retry":
            return
        if action == "cancel":
            raise SkipJob("user_cancelled")
        raise SkipJob("needs_user_action", message, keep_saved=True)

    async def _read(self, job: SavedJob, tab: BrowserTab, *, tolerate_blocked: bool = False) -> Extraction:
        """Read a job page, involving the user for as long as it is blocked."""
        while True:
            self._check()
            extraction = await extract_page(tab.page())
            if not extraction.blocked or tolerate_blocked:
                return extraction
            message = BLOCK_MESSAGES.get(extraction.block_reason or "", "page needs your attention")
            await self._blocked(job, message)

    async def _resolve(self, job: SavedJob) -> ReviewData:
        tab = self._open(job.url)
        await self._load(job, tab)
        self._check()

        if site_of(job.url) != "jobright":
            page = await self._read(job, tab)
            if page.is_linkedin or site_of(page.url) == "linkedin":
                raise SkipJob("linkedin")
            if not page.has_jd:
                # A page that looks form-only but still has some text gets a second opinion
                # from the AI check (in `_process`); anything else ends here.
                if page.form_only and len(page.jd_text.strip()) >= MIN_JD_CHARS:
                    return ReviewData(page.company, page.role, page.jd_text, page.url or job.url, None, form_only=True)
                if page.form_only:
                    raise SkipJob("form_only_no_jd")
                raise SkipJob("extraction_failed", "no job description was found on the page")
            return ReviewData(page.company, page.role, page.jd_text, page.url or job.url, None)

        # ---- a jobright job: read jobright's own description first ----
        jobright = await self._read(job, tab)
        if jobright.linkedin_source:
            raise SkipJob("linkedin")
        jobright_jd = jobright.jd_text if jobright.has_jd else (job.jd_text or "")
        role = jobright.role or job.role or ""
        company = jobright.company or job.company or ""
        fallback = ReviewData(company, role, jobright_jd, jobright.url or job.url, None)

        self.host.set_step(1, "Opening the real job site…")
        before = set(self.host.browser.views())
        # The Apply control is only ever pressed on jobright's own page (it opens the real job
        # site). On any other site a button called "Apply" is the application itself.
        on_jobright = site_of(tab.current_url()) == "jobright"
        if not on_jobright or not await click_apply_control(tab.page()):
            self.host.log("No Apply button found; using JobRight's description.")
            return self._require_jd(fallback)

        real_tab = await self._wait_new_tab(before, 8.0)
        if real_tab is None and site_of(tab.current_url()) != "jobright":
            real_tab = tab  # Apply went to the real site in the same tab
        if real_tab is None:
            self.host.log("Apply did not open the real site; using JobRight's description.")
            return self._require_jd(fallback)
        await self._race(real_tab.wait_loaded(self.ctx.config.page_load_timeout_seconds))
        self._check()
        if site_of(real_tab.current_url()) == "linkedin":
            raise SkipJob("linkedin")

        # A blocked or form-only real site is not a problem here: jobright's description stands in.
        real = await self._read(job, real_tab, tolerate_blocked=True)
        if real.is_linkedin:
            raise SkipJob("linkedin")
        real_url = real.url or real_tab.current_url()
        if real.has_jd and not real.blocked:
            return ReviewData(real.company or company, real.role or role, real.jd_text, real_url, real_url)

        self.host.browser.show_tab(tab)
        fallback.apply_url = real_url if real_url.startswith(WEB_SCHEMES) else None
        return self._require_jd(fallback)

    @staticmethod
    def _require_jd(data: ReviewData) -> ReviewData:
        if len(data.jd_text.strip()) < 100:
            raise SkipJob("extraction_failed", "no job description was found on the page")
        return data

    async def _wait_new_tab(self, before: set[BrowserTab], timeout: float) -> BrowserTab | None:
        waited = 0.0
        while waited < timeout:
            fresh = [view for view in self.host.browser.views() if view not in before]
            if fresh:
                return fresh[-1]
            await self._sleep(0.2)
            waited += 0.2
        return None

    # -- step 2: ChatGPT ----------------------------------------------------------------

    async def _chat_ready(self, tab: BrowserTab) -> None:
        """Wait for ChatGPT's message box. A login or challenge page pauses the run for the user."""
        while True:
            self._check()
            try:
                await chatgpt.wait_for_composer(tab.page(), timeout=45.0, cancel=self.cancel)
                return
            except chatgpt.ChatGPTBlocked as blocked:
                message = (
                    "ChatGPT is asking you to sign in."
                    if blocked.reason == "login"
                    else "ChatGPT is asking you to verify you are human."
                )
                self.host.log(f"Paused: {message}")
                # Every following job would fail too, so this pauses the run in both modes.
                if await self.host.chatgpt_blocked(message) != "retry":
                    raise Stopped() from blocked

    async def _generate(self, job: SavedJob, data: ReviewData) -> tuple[str, BrowserTab]:
        host = self.host
        assets = self.ctx.store.assets()
        prompt = compose_prompt(
            prompt_text=assets.prompt_text or "",
            original_resume_text=assets.original_resume_text,
            candidate_name=self.ctx.candidate_name,
            role=data.role,
            company=data.company,
            job_url=data.jd_url,
            jd_text=data.jd_text,
            output_schema=self.ctx.output_schema,
        )

        host.set_step(2, "Opening ChatGPT…")
        tab = self._open(self.chatgpt_url)
        if not await self._race(tab.wait_loaded(self.ctx.config.page_load_timeout_seconds)):
            raise SkipJob("chatgpt_failed", "ChatGPT did not load")
        self._check()

        host.log(f"Prompt: {len(prompt):,} characters")
        reply = await self._exchange(tab, prompt)
        host.log(f"Reply: {len(reply):,} characters")
        await self._step_delay()  # ② → ③
        return reply, tab

    async def _exchange(self, tab: BrowserTab, message: str) -> str:
        """Send one message in `tab` and return ChatGPT's reply.

        If ChatGPT shows a login or challenge page at ANY point (before, while pasting, or while
        answering), the run pauses for the user in both modes: every following job would fail the
        same way. The job is not skipped; after Retry the message is sent again.
        """
        host = self.host
        while True:
            self._check()
            try:
                await self._chat_ready(tab)
                baseline = await chatgpt.submit_prompt(
                    tab.page(), message, on_progress=lambda text: host.set_step(2, text), cancel=self.cancel
                )
                return await chatgpt.wait_for_reply(
                    tab.page(),
                    baseline,
                    float(self.ctx.config.chatgpt_reply_timeout_seconds),
                    on_progress=lambda text: host.set_step(2, text),
                    cancel=self.cancel,
                )
            except chatgpt.Cancelled as cancelled:
                raise Stopped() from cancelled
            except chatgpt.ChatGPTBlocked as blocked:
                text = (
                    "ChatGPT is asking you to sign in."
                    if blocked.reason == "login"
                    else "ChatGPT is asking you to verify you are human."
                )
                host.log(f"Paused: {text}")
                if await host.chatgpt_blocked(text) != "retry":
                    raise Stopped() from blocked
            except chatgpt.ReplyTimeout as error:
                raise SkipJob("chatgpt_failed", "ChatGPT did not finish its reply in time") from error
            except chatgpt.ChatGPTError as error:
                # The driver's messages are fixed sentences (no page or prompt text).
                raise SkipJob("chatgpt_failed", str(error)[:200]) from error

    # -- step 3: validate ---------------------------------------------------------------

    async def _ask_web(self, reply: str) -> dict[str, Any]:
        """Validate through the web app. If it is unreachable the run waits here; nothing unvalidated is saved."""
        while True:
            self._check()
            try:
                return await self._race(self.ctx.api.validate_resume(reply))
            except ApiError as error:
                self.host.log("Paused: the web app could not be reached for validation.")
                if await self.host.web_unreachable(error.message) != "retry":
                    raise Stopped() from error

    async def _validate(self, reply: str, tab: BrowserTab) -> tuple[dict[str, Any], BrowserTab]:
        host = self.host
        for attempt in range(1, MAX_JSON_ATTEMPTS + 1):
            self._check()
            host.set_step(3, f"Validating the resume (attempt {attempt} of {MAX_JSON_ATTEMPTS})…")

            if not chatgpt.contains_json_object(reply):
                issues: list[dict[str, Any]] = [
                    {"path": "(root)", "message": "The reply does not contain a JSON object."}
                ]
            else:
                result = await self._ask_web(reply[: self.ctx.limits["modelOutputMax"]])
                if result.get("ok") and isinstance(result.get("resume"), dict):
                    # The NORMALISED resume the web app returned, not ChatGPT's raw text.
                    return dict(result["resume"]), tab
                issues = [item for item in result.get("issues") or [] if isinstance(item, dict)]

            host.log(f"Validation failed (attempt {attempt} of {MAX_JSON_ATTEMPTS}): {len(issues)} problem(s)")
            last = attempt == MAX_JSON_ATTEMPTS
            if self.mode == "human":
                action = await host.json_failed(attempt, issues, reply, self.mode)
                self._check()
                if action != "retry" or last:
                    raise SkipJob("invalid_json", f"{len(issues)} problem(s) after {attempt} attempt(s)")
            elif last:
                raise SkipJob("invalid_json", f"{len(issues)} problem(s) after {attempt} attempts")

            # Ask for a corrected reply in the SAME conversation.
            host.set_step(2, f"Asking ChatGPT to correct the JSON (attempt {attempt + 1} of {MAX_JSON_ATTEMPTS})…")
            self.host.browser.show_tab(tab)
            reply = await self._exchange(tab, compose_retry_message(issues))

        raise SkipJob("invalid_json")

    async def _conversation_url(self, tab: BrowserTab) -> str | None:
        """The canonical https://chatgpt.com/c/<id> link, once the tab's address becomes one."""
        for _ in range(25):
            url = chatgpt.conversation_url(tab.page())
            if url:
                return url[: self.ctx.limits["chatUrlMax"]]
            if self.cancel.is_set():
                break
            await asyncio.sleep(0.2)
        return None


@dataclass
class _Facts:
    """What the Groq check is told about the page."""

    data: ReviewData
    title: str
    form_field_count: int = 0
    has_jobposting_ldjson: bool = False

    @property
    def url(self) -> str:
        return self.data.jd_url

    @property
    def role(self) -> str:
        return self.data.role

    @property
    def company(self) -> str:
        return self.data.company

    @property
    def jd_text(self) -> str:
        return self.data.jd_text
