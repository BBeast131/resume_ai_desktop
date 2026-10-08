"""The web app's API, as the desktop sees it.

Every request carries the signed-in user's own access token. The web app is
the authority on formats: it validates resume JSON, renders PDF and DOCX, runs
the Groq check and stores synced records.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote

import httpx

from app.services.auth import AuthError, AuthService


class ApiError(Exception):
    def __init__(self, message: str, code: str = "API_ERROR", status: int = 0, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.details = details


class OfflineError(ApiError):
    """The web app could not be reached at all."""

    def __init__(self, message: str = "The web app could not be reached. Check your internet connection.") -> None:
        super().__init__(message, "OFFLINE", 0)


def filename_from_disposition(header: str | None, fallback: str) -> str:
    """The server's own file name: `Full Name_Role_Company.pdf`."""
    if not header:
        return fallback
    star = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", header, flags=re.IGNORECASE)
    if star:
        name = unquote(star.group(1).strip())
    else:
        plain = re.search(r'filename\s*=\s*"?([^";]+)"?', header, flags=re.IGNORECASE)
        name = plain.group(1).strip() if plain else fallback
    # Never let a header choose a folder.
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return name or fallback


class WebApi:
    def __init__(self, base_url: str, auth: AuthService, http: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))

    def with_base_url(self, base_url: str) -> WebApi:
        """The same session against another address (Settings → Test connection)."""
        return WebApi(base_url, self.auth, self.http)

    async def _send(
        self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        for attempt in (1, 2):
            try:
                token = await self.auth.access_token()
            except AuthError as error:
                if error.code == "network":
                    raise OfflineError() from error
                raise ApiError(error.message, "UNAUTHENTICATED", 401) from error

            try:
                response = await self.http.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json,
                    params=params,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
            except httpx.HTTPError as error:
                raise OfflineError() from error
            except (httpx.InvalidURL, ValueError, TypeError) as error:
                raise ApiError("The web app URL in Settings is not a valid address.", "BAD_URL") from error

            if response.status_code == 401 and attempt == 1:
                # The token expired between the check and the request: refresh once.
                try:
                    await self.auth.refresh()
                except AuthError as error:
                    if error.code == "network":
                        raise OfflineError() from error
                    raise ApiError(error.message, "UNAUTHENTICATED", 401) from error
                continue
            return response
        raise ApiError("Your session has ended. Sign in again.", "UNAUTHENTICATED", 401)

    @staticmethod
    def _error(response: httpx.Response) -> ApiError:
        code, message, details = "API_ERROR", "", None
        try:
            payload = response.json()
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                code = str(error.get("code") or code)
                message = str(error.get("message") or "")
                details = error.get("details")
        except ValueError:
            pass
        if not message:
            if response.status_code == 404:
                message = "This web app does not have the desktop API yet. Deploy the latest version first."
                code = "NOT_FOUND"
            elif response.status_code == 413:
                message = "That request was too large for the web app."
            elif response.status_code >= 500:
                message = "The web app had a problem. Try again in a moment."
            else:
                message = f"The web app refused the request ({response.status_code})."
        return ApiError(message, code, response.status_code, details)

    async def _json(self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None) -> Any:
        response = await self._send(method, path, json=json, params=params)
        if response.status_code >= 400:
            raise self._error(response)
        try:
            payload = response.json()
        except ValueError as error:
            raise ApiError("The web app sent an unreadable answer.", "BAD_RESPONSE", response.status_code) from error
        if not isinstance(payload, dict) or not payload.get("success"):
            raise self._error(response)
        return payload.get("data")

    # -- account ---------------------------------------------------------------

    async def get_account(self) -> dict[str, Any]:
        data = await self._json("GET", "/api/account")
        return dict(data or {})

    # -- desktop ---------------------------------------------------------------

    async def get_config(self) -> dict[str, Any]:
        return dict(await self._json("GET", "/api/desktop/config") or {})

    async def validate_resume(self, model_output: str) -> dict[str, Any]:
        return dict(await self._json("POST", "/api/desktop/validate-resume", json={"modelOutput": model_output}) or {})

    async def render(self, resume: Any, company: str | None, role: str | None, fmt: str) -> tuple[bytes, str]:
        response = await self._send(
            "POST", "/api/desktop/render", json={"resume": resume, "company": company, "role": role, "format": fmt}
        )
        if response.status_code >= 400:
            raise self._error(response)
        name = filename_from_disposition(response.headers.get("content-disposition"), f"Resume.{fmt}")
        return response.content, name

    async def extract_validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(await self._json("POST", "/api/desktop/extract-validate", json=payload) or {})

    async def sync_push(self, body: dict[str, Any]) -> dict[str, Any]:
        return dict(await self._json("POST", "/api/desktop/sync", json=body) or {})

    async def sync_pull(self, since: str | None, *, slim: bool, limit: int = 100) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "slim": "1" if slim else "0"}
        if since:
            params["since"] = since
        return dict(await self._json("GET", "/api/desktop/sync", params=params) or {})

    # -- the shared job list (every profile's finds) ----------------------------

    async def public_jobs_push(self, jobs: list[dict[str, Any]]) -> dict[str, Any]:
        return dict(await self._json("POST", "/api/desktop/public-jobs", json={"jobs": jobs}) or {})

    async def public_jobs_pull(self, *, after: int, since: str | None = None, limit: int = 50) -> dict[str, Any]:
        params: dict[str, Any] = {"after": after, "limit": limit}
        if since:
            params["since"] = since
        return dict(await self._json("GET", "/api/desktop/public-jobs", params=params) or {})

    # -- admin (read-only; the server checks the role again) ---------------------

    async def admin_users(self, search: str = "", page: int = 1, page_size: int = 100) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if search.strip():
            params["search"] = search.strip()
        return dict(await self._json("GET", "/api/admin/users", params=params) or {})

    async def admin_user_desktop(self, user_id: str) -> dict[str, Any]:
        return dict(await self._json("GET", f"/api/admin/users/{user_id}/desktop") or {})

    async def admin_user_resume(self, user_id: str, resume_id: str) -> dict[str, Any]:
        return dict(await self._json("GET", f"/api/admin/users/{user_id}/desktop", params={"resume": resume_id}) or {})

    async def close(self) -> None:
        await self.http.aclose()
