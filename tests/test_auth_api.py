"""Sign-in against Supabase Auth and the web API client, with HTTP mocked."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.config import AppConfig
from app.services.auth import AuthError, AuthService, MemoryVault, load_last_user
from app.sync.api_client import ApiError, OfflineError, WebApi, filename_from_disposition

SUPABASE = "https://project.supabase.co"
WEB = "https://web.example"
CONFIG = AppConfig(web_app_url=WEB, supabase_url=SUPABASE, supabase_publishable_key="sb_publishable_test")
USER = {
    "id": "11111111-1111-4111-8111-111111111111",
    "email": "a@example.com",
    "user_metadata": {"full_name": "Anthony Fox"},
}


def token(access: str = "access-1", refresh: str = "refresh-1", expires_in: int = 3600) -> dict:
    return {"access_token": access, "refresh_token": refresh, "expires_in": expires_in, "user": USER}


def make_auth(vault: MemoryVault | None = None) -> AuthService:
    return AuthService(CONFIG, vault=vault or MemoryVault())


@respx.mock
async def test_sign_in_uses_the_password_grant_and_keeps_the_refresh_token_in_the_vault():
    route = respx.post(f"{SUPABASE}/auth/v1/token").mock(return_value=httpx.Response(200, json=token()))
    vault = MemoryVault()
    auth = make_auth(vault)

    session = await auth.sign_in("a@example.com ", "pw")

    request = route.calls.last.request
    assert request.url.params["grant_type"] == "password"
    assert request.headers["apikey"] == "sb_publishable_test"
    assert json.loads(request.content) == {"email": "a@example.com", "password": "pw"}
    assert session.user.full_name == "Anthony Fox" and session.user.initials == "AF"
    assert vault.token == "refresh-1"
    # The non-secret "who was signed in" record never holds a token.
    assert load_last_user().email == "a@example.com"


@respx.mock
async def test_wrong_password_is_reported_in_plain_words():
    respx.post(f"{SUPABASE}/auth/v1/token").mock(
        return_value=httpx.Response(400, json={"error_code": "invalid_credentials", "msg": "Invalid login credentials"})
    )
    with pytest.raises(AuthError) as caught:
        await make_auth().sign_in("a@example.com", "bad")
    assert caught.value.code == "invalid_credentials"
    assert caught.value.message == "The e-mail or password is not correct."


@respx.mock
async def test_sign_up_sends_full_name_as_metadata():
    route = respx.post(f"{SUPABASE}/auth/v1/signup").mock(return_value=httpx.Response(200, json=token()))
    session = await make_auth().sign_up(" Anthony Fox ", "a@example.com", "password123")
    body = json.loads(route.calls.last.request.content)
    assert body["data"] == {"full_name": "Anthony Fox"}
    assert "role" not in json.dumps(body)
    assert session is not None


@respx.mock
async def test_sign_up_that_needs_email_confirmation_returns_none():
    respx.post(f"{SUPABASE}/auth/v1/signup").mock(
        return_value=httpx.Response(200, json={"id": "x", "email": "a@example.com"})
    )
    assert await make_auth().sign_up("Anthony Fox", "a@example.com", "password123") is None


@respx.mock
async def test_existing_account_and_network_errors():
    respx.post(f"{SUPABASE}/auth/v1/signup").mock(
        return_value=httpx.Response(422, json={"error_code": "user_already_exists", "msg": "User already registered"})
    )
    with pytest.raises(AuthError) as caught:
        await make_auth().sign_up("A B", "a@example.com", "password123")
    assert caught.value.code == "user_exists"

    respx.post(f"{SUPABASE}/auth/v1/token").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(AuthError) as caught:
        await make_auth().sign_in("a@example.com", "pw")
    assert caught.value.code == "network"


async def test_missing_configuration_is_explained():
    auth = AuthService(AppConfig(), vault=MemoryVault())
    with pytest.raises(AuthError) as caught:
        await auth.sign_in("a@example.com", "pw")
    assert caught.value.code == "config" and "SUPABASE_URL" in caught.value.message


@respx.mock
async def test_stays_signed_in_through_the_saved_refresh_token():
    respx.post(f"{SUPABASE}/auth/v1/token").mock(return_value=httpx.Response(200, json=token("access-2", "refresh-2")))
    vault = MemoryVault("refresh-1")
    auth = make_auth(vault)
    assert await auth.access_token() == "access-2"
    assert vault.token == "refresh-2"  # rotated


@respx.mock
async def test_a_dead_refresh_token_signs_out():
    respx.post(f"{SUPABASE}/auth/v1/token").mock(
        return_value=httpx.Response(400, json={"error_code": "refresh_token_not_found", "msg": "Invalid Refresh Token"})
    )
    vault = MemoryVault("stale")
    auth = make_auth(vault)
    with pytest.raises(AuthError) as caught:
        await auth.access_token()
    assert caught.value.code == "session_expired" and vault.token is None


@respx.mock
async def test_logout_clears_the_vault_even_when_offline():
    respx.post(f"{SUPABASE}/auth/v1/token").mock(return_value=httpx.Response(200, json=token()))
    respx.post(f"{SUPABASE}/auth/v1/logout").mock(side_effect=httpx.ConnectError("down"))
    vault = MemoryVault()
    auth = make_auth(vault)
    await auth.sign_in("a@example.com", "pw")
    await auth.sign_out()
    assert vault.token is None and auth.session is None and load_last_user() is None


def test_a_secret_key_is_refused_by_the_config(monkeypatch):
    from app.config import load_config

    monkeypatch.setenv("SUPABASE_URL", SUPABASE)
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_secret_abcdefgh")
    config = load_config()
    assert config.supabase_publishable_key == "" and not config.auth_configured
    assert any("SECRET" in problem for problem in config.problems)


# ---------------------------------------------------------------------------


async def signed_in_api() -> WebApi:
    auth = make_auth(MemoryVault("refresh-1"))
    return WebApi(WEB, auth)


@respx.mock
async def test_requests_carry_the_users_bearer_token():
    respx.post(f"{SUPABASE}/auth/v1/token").mock(return_value=httpx.Response(200, json=token()))
    route = respx.get(f"{WEB}/api/desktop/config").mock(
        return_value=httpx.Response(200, json={"success": True, "data": {"outputSchema": "{}"}})
    )
    api = await signed_in_api()
    assert await api.get_config() == {"outputSchema": "{}"}
    assert route.calls.last.request.headers["authorization"] == "Bearer access-1"


@respx.mock
async def test_an_expired_token_is_refreshed_once_and_the_request_repeated():
    respx.post(f"{SUPABASE}/auth/v1/token").mock(
        side_effect=[httpx.Response(200, json=token("old", "r1")), httpx.Response(200, json=token("new", "r2"))]
    )
    route = respx.get(f"{WEB}/api/account").mock(
        side_effect=[
            httpx.Response(401, json={"success": False, "error": {"code": "TOKEN_EXPIRED", "message": "expired"}}),
            httpx.Response(200, json={"success": True, "data": {"profile": {"role": "admin"}}}),
        ]
    )
    api = await signed_in_api()
    assert (await api.get_account())["profile"]["role"] == "admin"
    assert [call.request.headers["authorization"] for call in route.calls] == ["Bearer old", "Bearer new"]


@respx.mock
async def test_errors_carry_the_servers_code_and_message():
    respx.post(f"{SUPABASE}/auth/v1/token").mock(return_value=httpx.Response(200, json=token()))
    respx.post(f"{WEB}/api/desktop/sync").mock(
        return_value=httpx.Response(
            429, json={"success": False, "error": {"code": "RATE_LIMITED", "message": "Too many requests."}}
        )
    )
    api = await signed_in_api()
    with pytest.raises(ApiError) as caught:
        await api.sync_push({})
    assert (caught.value.code, caught.value.status, caught.value.message) == ("RATE_LIMITED", 429, "Too many requests.")


@respx.mock
async def test_unreachable_web_app_is_offline_not_an_error():
    respx.post(f"{SUPABASE}/auth/v1/token").mock(return_value=httpx.Response(200, json=token()))
    respx.get(f"{WEB}/api/desktop/config").mock(side_effect=httpx.ConnectError("down"))
    api = await signed_in_api()
    with pytest.raises(OfflineError):
        await api.get_config()


@respx.mock
async def test_a_web_app_without_the_desktop_api_says_so():
    respx.post(f"{SUPABASE}/auth/v1/token").mock(return_value=httpx.Response(200, json=token()))
    respx.get(f"{WEB}/api/desktop/config").mock(return_value=httpx.Response(404, text="<html>not found</html>"))
    api = await signed_in_api()
    with pytest.raises(ApiError) as caught:
        await api.get_config()
    assert "Deploy the latest version" in caught.value.message


@respx.mock
async def test_render_returns_the_servers_filename():
    respx.post(f"{SUPABASE}/auth/v1/token").mock(return_value=httpx.Response(200, json=token()))
    respx.post(f"{WEB}/api/desktop/render").mock(
        return_value=httpx.Response(
            200,
            content=b"%PDF-1.7 test",
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="Anthony Fox_Staff Engineer_Acme.pdf"; '
                "filename*=UTF-8''Anthony%20Fox_Staff%20Engineer_Acme.pdf",
            },
        )
    )
    api = await signed_in_api()
    content, name = await api.render({"contact": {}}, "Acme", "Staff Engineer", "pdf")
    assert content.startswith(b"%PDF") and name == "Anthony Fox_Staff Engineer_Acme.pdf"


def test_a_filename_header_can_never_choose_a_folder():
    name = filename_from_disposition('attachment; filename="..\\..\\evil.pdf"', "Resume.pdf")
    assert "/" not in name and "\\" not in name and not name.startswith(".")
    name = filename_from_disposition("attachment; filename*=UTF-8''..%2F..%2Fevil.pdf", "Resume.pdf")
    assert "/" not in name and "\\" not in name
    assert filename_from_disposition(None, "Resume.pdf") == "Resume.pdf"
