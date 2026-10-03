"""Accounts: the same Supabase Auth accounts the web app uses.

The app talks to Supabase Auth (GoTrue) over REST with the project URL and
the *publishable* key, both public values. It never sees a service-role key.

The refresh token is kept in the operating system's credential store
(Windows Credential Manager) through `keyring`, never in a plain file. A
small, non-secret record of who was signed in (id, e-mail, name, role) is
kept beside the data so the app can open on local data with no internet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from app.config import APP_NAME, AppConfig, app_data_dir

log = logging.getLogger(__name__)

KEYRING_SERVICE = APP_NAME
KEYRING_USER = "refresh_token"


class AuthError(Exception):
    """A sign-in problem, already worded for the user."""

    def __init__(self, message: str, code: str = "auth_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class UserProfile:
    id: str
    email: str
    full_name: str
    role: str = "user"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def initials(self) -> str:
        parts = [part for part in (self.full_name or self.email).replace("@", " ").split() if part]
        letters = "".join(part[0] for part in parts[:2]).upper()
        return letters or "?"


@dataclass
class Session:
    access_token: str
    refresh_token: str
    expires_at: float
    user: UserProfile


class TokenVault:
    """Where the refresh token lives. The default is the OS credential store."""

    def load(self) -> str | None:
        try:
            import keyring

            return keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:  # no backend (a headless Linux box): stay signed in for this run only
            return None

    def save(self, token: str) -> None:
        try:
            import keyring

            keyring.set_password(KEYRING_SERVICE, KEYRING_USER, token)
        except Exception:
            log.warning("auth.keyring_unavailable")

    def clear(self) -> None:
        try:
            import keyring

            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            pass


class MemoryVault(TokenVault):
    """For tests."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token

    def load(self) -> str | None:
        return self.token

    def save(self, token: str) -> None:
        self.token = token

    def clear(self) -> None:
        self.token = None


def _last_user_path() -> Any:
    return app_data_dir() / "last_user.json"


def load_last_user() -> UserProfile | None:
    try:
        raw = json.loads(_last_user_path().read_text(encoding="utf-8"))
        return UserProfile(
            id=str(raw["id"]),
            email=str(raw["email"]),
            full_name=str(raw.get("full_name") or ""),
            role=str(raw.get("role") or "user"),
        )
    except (OSError, ValueError, KeyError):
        return None


def save_last_user(user: UserProfile | None) -> None:
    path = _last_user_path()
    try:
        if user is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(user)), encoding="utf-8")
    except OSError:
        pass


def _plain_error(payload: Any, status: int) -> AuthError:
    """Supabase's error, in plain words. The raw message is never shown: it can echo input."""
    text = ""
    code = ""
    if isinstance(payload, dict):
        text = str(payload.get("msg") or payload.get("error_description") or payload.get("message") or "")
        code = str(payload.get("error_code") or payload.get("error") or "")
    lowered = f"{text} {code}".lower()

    if "invalid login" in lowered or "invalid_credentials" in lowered or "invalid_grant" in lowered:
        return AuthError("The e-mail or password is not correct.", "invalid_credentials")
    if "not confirmed" in lowered or "email_not_confirmed" in lowered:
        return AuthError("Check your inbox to confirm your e-mail, then sign in.", "email_not_confirmed")
    if "already registered" in lowered or "already exists" in lowered or "user_already_exists" in lowered:
        return AuthError("An account with that e-mail already exists. Sign in instead.", "user_exists")
    if "weak" in lowered or ("password" in lowered and "least" in lowered):
        return AuthError("That password is too weak. Use at least 8 characters.", "weak_password")
    if "signup" in lowered and "disabled" in lowered:
        return AuthError("Creating new accounts is turned off for this app.", "signup_disabled")
    if status == 429 or "rate limit" in lowered:
        return AuthError("Too many attempts. Wait a minute and try again.", "rate_limited")
    if "refresh" in lowered or "session_not_found" in lowered or "session not found" in lowered:
        return AuthError("Your session has ended. Sign in again.", "session_expired")
    if status in (401, 403):
        # Not the user's session: the project URL or the publishable key is wrong, or a proxy refused.
        return AuthError(
            "The sign-in service refused the app. Check SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY in .env.",
            "refused",
        )
    return AuthError("The sign-in service did not respond as expected. Try again.", "auth_error")


class AuthService:
    def __init__(
        self,
        config: AppConfig,
        http: httpx.AsyncClient | None = None,
        vault: TokenVault | None = None,
    ) -> None:
        self.config = config
        self.http = http or httpx.AsyncClient(timeout=20.0)
        self.vault = vault or TokenVault()
        self.session: Session | None = None
        self._refresh_lock = asyncio.Lock()

    # -- helpers -------------------------------------------------------------

    def _require_config(self) -> None:
        if not self.config.auth_configured:
            raise AuthError(
                "The app is not set up yet: SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY are missing from .env.",
                "config",
            )

    def _headers(self, bearer: str | None = None) -> dict[str, str]:
        key = self.config.supabase_publishable_key
        return {"apikey": key, "Authorization": f"Bearer {bearer or key}", "Content-Type": "application/json"}

    async def _post(self, path: str, body: dict[str, Any], bearer: str | None = None) -> Any:
        self._require_config()
        try:
            response = await self.http.post(
                f"{self.config.supabase_url}/auth/v1{path}", json=body, headers=self._headers(bearer)
            )
        except httpx.HTTPError as error:
            raise AuthError("No connection to the sign-in service. Check your internet.", "network") from error

        payload: Any = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code >= 400:
            raise _plain_error(payload, response.status_code)
        return payload

    def _adopt(self, payload: dict[str, Any], role: str = "user") -> Session:
        user = payload.get("user") or {}
        metadata = user.get("user_metadata") or {}
        email = str(user.get("email") or "")
        previous = self.session.user if self.session else load_last_user()
        if previous is not None and previous.id == str(user.get("id")):
            role = previous.role  # the real role comes from the web app's profile; keep what we know
        profile = UserProfile(
            id=str(user.get("id") or ""),
            email=email,
            full_name=str(metadata.get("full_name") or email.split("@")[0]),
            role=role,
        )
        expires_in = float(payload.get("expires_in") or 3600)
        self.session = Session(
            access_token=str(payload["access_token"]),
            refresh_token=str(payload["refresh_token"]),
            expires_at=time.time() + expires_in,
            user=profile,
        )
        self.vault.save(self.session.refresh_token)
        save_last_user(profile)
        return self.session

    # -- public --------------------------------------------------------------

    async def sign_in(self, email: str, password: str) -> Session:
        payload = await self._post("/token?grant_type=password", {"email": email.strip(), "password": password})
        return self._adopt(payload)

    async def sign_up(self, full_name: str, email: str, password: str) -> Session | None:
        """Create a web-app account. Returns None when e-mail confirmation is required."""
        payload = await self._post(
            "/signup",
            {
                "email": email.strip(),
                "password": password,
                # full_name is read by the web app's handle_new_user trigger. The role is never
                # taken from this metadata: the trigger always writes 'user'.
                "data": {"full_name": full_name.strip()},
            },
        )
        if isinstance(payload, dict) and payload.get("access_token"):
            return self._adopt(payload)
        return None

    async def refresh(self) -> Session:
        async with self._refresh_lock:
            token = self.session.refresh_token if self.session else self.vault.load()
            if not token:
                raise AuthError("Your session has ended. Sign in again.", "session_expired")
            try:
                payload = await self._post("/token?grant_type=refresh_token", {"refresh_token": token})
            except AuthError as error:
                # Only a refresh token the server no longer accepts ends the session. A wrong
                # key in .env or a proxy error must not throw the saved sign-in away.
                if error.code in ("session_expired", "invalid_credentials"):
                    self.vault.clear()
                    self.session = None
                    raise AuthError("Your session has ended. Sign in again.", "session_expired") from error
                raise
            return self._adopt(payload)

    async def access_token(self) -> str:
        """A token that is good for at least the next minute."""
        if self.session is None or self.session.expires_at - time.time() < 60:
            await self.refresh()
        assert self.session is not None
        return self.session.access_token

    def has_saved_session(self) -> bool:
        return bool(self.vault.load()) and load_last_user() is not None

    def set_role(self, role: str, full_name: str | None = None) -> None:
        """Record what the web app's profile says. The role is display-only here: every
        admin request is checked again on the server."""
        if self.session is None:
            return
        self.session.user.role = role
        if full_name:
            self.session.user.full_name = full_name
        save_last_user(self.session.user)

    async def sign_out(self) -> None:
        session = self.session
        self.session = None
        self.vault.clear()
        save_last_user(None)
        if session is not None:
            try:
                # scope=local: end THIS session only, not the web app's or the extension's.
                await self._post("/logout?scope=local", {}, bearer=session.access_token)
            except AuthError:
                pass  # the local sign-out already happened

    async def close(self) -> None:
        await self.http.aclose()
