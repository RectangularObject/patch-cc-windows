"""OpenAI ChatGPT/Codex-plan OAuth: device-code sign-in, token storage, refresh.

The one provider patch-cc bridges. The flow is OpenAI's public Codex device-code
grant (the same client id the Codex CLI uses); credentials live in a 0600 file
under ``XDG_DATA_HOME`` -- never in the model config, never sent to Anthropic.
Network calls use the standard library, so this adds no dependency.

The token is a ChatGPT OAuth JWT, *not* an ``sk-`` API key: it is accepted by
``chatgpt.com/backend-api/codex`` and rejected by ``api.openai.com``. That is
why discovery and inference both target the ChatGPT backend, and why the account
id -- carried in a ``ChatGPT-Account-Id`` header -- is read out of the JWT's own
claims rather than fetched.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: OpenAI's public Codex client id (device-code grant). Not a secret.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"

#: Refresh this many seconds before nominal expiry, so an in-flight request
#: never rides a token that lapses mid-call.
_REFRESH_SKEW = 120
_HTTP_TIMEOUT = 30


class OAuthError(RuntimeError):
    """A sign-in or refresh step failed in a way worth showing the user.

    ``status`` carries the HTTP code when the failure was one. Keeping it here is
    what lets this be the *only* exception the module raises: the sign-in poll
    reads the field to tell "not yet" from "no", so no caller has to catch a raw
    ``HTTPError`` -- which is how a rotated-out refresh token used to surface as
    ``HTTP Error 400: Bad Request`` in the CLI, the menu, and mid-turn in Claude
    Code, none of them saying the one thing that fixes it.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ── credential model + storage ───────────────────────────────────────────────


@dataclass(slots=True)
class Credentials:
    access: str
    refresh: str
    #: Epoch seconds when ``access`` lapses.
    expires: float
    account_id: str | None = None

    def expiring(self, now: float | None = None) -> bool:
        return self.expires <= (now if now is not None else time.time()) + _REFRESH_SKEW

    def to_json(self) -> dict[str, object]:
        return {
            "access": self.access,
            "refresh": self.refresh,
            "expires": self.expires,
            "account_id": self.account_id,
        }

    @classmethod
    def from_json(cls, data: object) -> Credentials | None:
        if not isinstance(data, dict):
            return None
        access, refresh = data.get("access"), data.get("refresh")
        expires = data.get("expires")
        if not isinstance(access, str) or not isinstance(refresh, str):
            return None
        account = data.get("account_id")
        return cls(
            access=access,
            refresh=refresh,
            expires=float(expires) if isinstance(expires, (int, float)) else 0.0,
            account_id=account if isinstance(account, str) else None,
        )


def auth_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "patch-cc" / "codex-auth.json"


def load() -> Credentials | None:
    try:
        return Credentials.from_json(json.loads(auth_path().read_text("utf8")))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def save(creds: Credentials) -> None:
    path = auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # Write restricted from the start; the token must never be world-readable.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf8") as handle:
        json.dump(creds.to_json(), handle)
    os.replace(tmp, path)


def logout() -> bool:
    """Delete stored credentials; ``True`` if a file was removed."""
    try:
        auth_path().unlink()
        return True
    except OSError:
        return False


# ── JWT claims (pure) ────────────────────────────────────────────────────────


def _decode_jwt_payload(token: str | None) -> dict[str, object] | None:
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return None


def account_id_from_tokens(
    id_token: str | None, access_token: str | None
) -> str | None:
    """The ChatGPT account id, in the precedence OpenAI's own claims use."""
    claims = _decode_jwt_payload(id_token) or _decode_jwt_payload(access_token) or {}
    direct = claims.get("chatgpt_account_id")
    if isinstance(direct, str):
        return direct
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict) and isinstance(auth.get("chatgpt_account_id"), str):
        return auth["chatgpt_account_id"]  # type: ignore[return-value]
    orgs = claims.get("organizations")
    if isinstance(orgs, list) and orgs and isinstance(orgs[0], dict):
        first = orgs[0].get("id")
        return first if isinstance(first, str) else None
    return None


def _expiry_seconds(expires_in: object, access_token: str | None) -> float:
    """Prefer the JWT's own ``exp``; fall back to ``expires_in`` (or one hour)."""
    claims = _decode_jwt_payload(access_token) or {}
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        return float(exp)
    seconds = (
        expires_in if isinstance(expires_in, (int, float)) and expires_in > 0 else 3600
    )
    return time.time() + float(seconds)


def _credentials_from_token_response(data: dict[str, object]) -> Credentials:
    access = data.get("access_token")
    if not isinstance(access, str):
        raise OAuthError("OpenAI did not return an access token")
    refresh = data.get("refresh_token")
    id_token = data.get("id_token")
    return Credentials(
        access=access,
        refresh=refresh if isinstance(refresh, str) else "",
        expires=_expiry_seconds(data.get("expires_in"), access),
        account_id=account_id_from_tokens(
            id_token if isinstance(id_token, str) else None, access
        ),
    )


# ── network ──────────────────────────────────────────────────────────────────


def _post(url: str, *, json_body: dict | None = None, form: dict | None = None) -> dict:
    """POST and parse, turning every failure into an ``OAuthError``.

    Converting at the one boundary they all cross means no caller has to guess
    which failures reach it: a refused connection, a stall, a captive portal's
    HTML where JSON was promised, a rejected grant. The HTTP status rides on the
    error rather than on a second exception type, so the one caller that reads it
    still can and the rest cannot accidentally not.
    """
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        content_type = "application/x-www-form-urlencoded"
    else:
        body = json.dumps(json_body or {}).encode()
        content_type = "application/json"
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": content_type,
            "Accept": "application/json",
            "User-Agent": "patch-cc",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise OAuthError(
            f"OpenAI refused the request (HTTP {exc.code})", exc.code
        ) from exc
    except (OSError, ValueError) as exc:
        # URLError is an OSError and JSONDecodeError a ValueError, so these two
        # cover the network, the socket timeout, and the unparseable reply.
        raise OAuthError(f"OpenAI request failed: {exc}") from exc


# ── device-code sign-in ──────────────────────────────────────────────────────


@dataclass(slots=True)
class DeviceCode:
    device_auth_id: str
    user_code: str
    verification_url: str
    interval: int
    expires_in: int


def request_device_code() -> DeviceCode:
    data = _post(
        f"{ISSUER}/api/accounts/deviceauth/usercode",
        json_body={"client_id": CLIENT_ID},
    )
    return DeviceCode(
        device_auth_id=str(data.get("device_auth_id", "")),
        user_code=str(data.get("user_code", "")),
        verification_url=f"{ISSUER}/codex/device",
        interval=int(data.get("interval", 5) or 5),
        expires_in=int(data.get("expires_in", 300) or 300),
    )


def poll_for_token(
    device: DeviceCode,
    *,
    sleep: Callable[[float], object] = time.sleep,
    now: Callable[[], float] = time.time,
    cancel: Callable[[], bool] | None = None,
) -> Credentials:
    """Block until the user authorizes the device code, then exchange it.

    A 403/404 while polling means "not yet"; anything else is a real failure.
    ``cancel`` is polled between attempts so a menu can abandon the wait.
    """
    deadline = now() + device.expires_in
    interval = max(device.interval, 1)
    while now() < deadline:
        if cancel is not None and cancel():
            raise OAuthError("sign-in cancelled")
        try:
            grant = _post(
                f"{ISSUER}/api/accounts/deviceauth/token",
                json_body={
                    "device_auth_id": device.device_auth_id,
                    "user_code": device.user_code,
                },
            )
        except OAuthError as exc:
            # The one status the poll reads rather than reports: still waiting.
            if exc.status not in (403, 404):
                raise
            sleep(interval)
            continue

        tokens = _post(
            f"{ISSUER}/oauth/token",
            form={
                "grant_type": "authorization_code",
                "code": str(grant.get("authorization_code", "")),
                "redirect_uri": f"{ISSUER}/deviceauth/callback",
                "client_id": CLIENT_ID,
                "code_verifier": str(grant.get("code_verifier", "")),
            },
        )
        return _credentials_from_token_response(tokens)
    raise OAuthError("OpenAI sign-in timed out; run `patch-cc codex login` again")


def refresh(creds: Credentials) -> Credentials:
    """Trade the stored refresh token for a live access token.

    A refused grant is the ordinary end of a stored session -- the token was
    rotated out, revoked, or the plan changed -- and it is the failure a user is
    most likely to meet, so it says the one thing that fixes it rather than
    passing OpenAI's bare status along.
    """
    if not creds.refresh:
        raise OAuthError("no refresh token; sign in again with `patch-cc codex login`")
    try:
        tokens = _post(
            f"{ISSUER}/oauth/token",
            form={
                "grant_type": "refresh_token",
                "refresh_token": creds.refresh,
                "client_id": CLIENT_ID,
            },
        )
    except OAuthError as exc:
        if exc.status is None:
            raise  # the network, not the grant: report it as it came
        raise OAuthError(
            "stored OpenAI session is no longer valid; sign in again with "
            "`patch-cc codex login`",
            exc.status,
        ) from exc
    refreshed = _credentials_from_token_response(tokens)
    # A refresh response often omits a new refresh token / account id; keep ours.
    if not refreshed.refresh:
        refreshed.refresh = creds.refresh
    if refreshed.account_id is None:
        refreshed.account_id = creds.account_id
    return refreshed


def valid_access(creds: Credentials) -> tuple[str, Credentials]:
    """Return a live access token, refreshing and re-persisting if it is expiring."""
    if not creds.expiring():
        return creds.access, creds
    refreshed = refresh(creds)
    save(refreshed)
    return refreshed.access, refreshed


def login(
    on_code: Callable[[str, str], object],
    *,
    cancel: Callable[[], bool] | None = None,
) -> Credentials:
    """Run the device-code flow start to finish, persisting the result.

    ``on_code(verification_url, user_code)`` fires once the code is issued so the
    caller -- CLI or menu -- shows it however it likes. Shared by both surfaces
    so the sign-in behaves identically whichever way it is reached.
    """
    device = request_device_code()
    on_code(device.verification_url, device.user_code)
    creds = poll_for_token(device, cancel=cancel)
    save(creds)
    return creds
