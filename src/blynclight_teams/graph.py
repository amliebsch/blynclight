"""Microsoft Graph presence via the device-code OAuth flow.

Presence.Read is a delegated permission. A background service cannot pop a
browser on every start, so login happens once and the refresh token is reused.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from blynclight_teams.config import Config
from blynclight_teams.errors import ConfigError, GraphTemporaryError, NotSignedIn

_TENANT_GUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_TENANT_ALIASES = {"organizations", "common", "consumers"}
_ISSUER = re.compile(r"^https://[^/]+/([^/]+)/v2\.0/?$")

SCOPES = " ".join(
    [
        "https://graph.microsoft.com/Presence.Read",
        "https://graph.microsoft.com/User.Read",
        "offline_access",
    ]
)
USER_AGENT = "blynclight-teams/0.1"

Http = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


@dataclass(frozen=True)
class Presence:
    availability: str
    activity: str

    @property
    def label(self) -> str:
        return f"{self.availability}/{self.activity}"


@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str = ""

    def expired(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return current >= self.expires_at - 120

    def to_json(self) -> dict[str, object]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
        }

    @classmethod
    def from_json(cls, payload: dict) -> Token:
        return cls(
            access_token=str(payload["access_token"]),
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_at=float(payload["expires_at"]),
            scope=str(payload.get("scope") or ""),
        )


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    message: str


def default_http(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise GraphTemporaryError(f"network error talking to {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise GraphTemporaryError(f"timed out talking to {url}") from exc


class GraphClient:
    def __init__(self, config: Config, http: Http = default_http, now: Callable[[], float] = time.time):
        self.config = config
        self._http = http
        self._now = now
        self._token: Token | None = None

    def login(self, sleep: Callable[[float], None] = time.sleep) -> tuple[Token, Presence, str]:
        """Run device-code login and confirm Presence.Read works. Returns token, presence, account label."""
        code = self.request_device_code()
        print(code.message)
        print(f"Open {code.verification_uri} and enter {code.user_code}")
        deadline = self._now() + code.expires_in
        interval = max(code.interval, 5)
        while self._now() < deadline:
            sleep(interval)
            token, pending = self.poll_device_code(code.device_code)
            if token is not None:
                self._store(token)
                account = self.account_label()
                presence = self.get_presence()
                return token, presence, account
            if pending == "slow_down":
                interval = min(interval + 5, 60)
            elif pending == "authorization_pending":
                continue
            else:
                raise _signin_error(pending or "sign-in did not complete")
        raise NotSignedIn("The device code expired before it was approved. Run login again.")

    def request_device_code(self) -> DeviceCode:
        payload = self._form(
            self._oauth_url("devicecode"),
            {"client_id": self.config.client_id, "scope": SCOPES},
        )
        self._raise_oauth_error(payload)
        if "device_code" not in payload or "user_code" not in payload:
            raise NotSignedIn("Microsoft did not return a device code.")
        return DeviceCode(
            device_code=str(payload["device_code"]),
            user_code=str(payload["user_code"]),
            verification_uri=str(payload.get("verification_uri") or "https://microsoft.com/devicelogin"),
            expires_in=int(payload.get("expires_in") or 900),
            interval=int(payload.get("interval") or 5),
            message=str(payload.get("message") or "Sign in with the code below."),
        )

    def poll_device_code(self, device_code: str) -> tuple[Token | None, str | None]:
        """Return (token, None) when approved, or (None, error) while waiting."""
        payload = self._form(
            self._oauth_url("token"),
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self.config.client_id,
                "device_code": device_code,
            },
        )
        error = str(payload.get("error") or "")
        if error in {"authorization_pending", "slow_down"}:
            return None, error
        if error:
            description = str(payload.get("error_description") or error)
            return None, description
        if "access_token" not in payload:
            return None, "Microsoft did not return an access token."
        return self._token_from_oauth(payload), None

    def get_presence(self) -> Presence:
        status, body = self._authorized("GET", "https://graph.microsoft.com/v1.0/me/presence")
        if status == 403:
            raise NotSignedIn(
                "Microsoft Graph refused Presence.Read (HTTP 403). "
                "An admin needs to grant consent for the app registration's "
                "Presence.Read permission, then run login again."
            )
        if status == 401:
            raise NotSignedIn("Microsoft Graph rejected the access token. Run login again.")
        if status != 200:
            raise GraphTemporaryError(f"GET /me/presence returned HTTP {status}: {_snippet(body)}")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise GraphTemporaryError("Graph returned a presence document that is not JSON") from exc
        return Presence(
            availability=str(payload.get("availability") or ""),
            activity=str(payload.get("activity") or ""),
        )

    def account_label(self) -> str:
        try:
            status, body = self._authorized("GET", "https://graph.microsoft.com/v1.0/me")
        except GraphTemporaryError:
            return ""
        if status != 200:
            return ""
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return ""
        name = str(payload.get("displayName") or "").strip()
        mail = str(payload.get("mail") or payload.get("userPrincipalName") or "").strip()
        if name and mail:
            return f"{name} <{mail}>"
        return mail or name

    def _authorized(self, method: str, url: str) -> tuple[int, bytes]:
        token = self._valid_token()
        status, body = self._raw(
            method,
            url,
            {"Authorization": f"Bearer {token.access_token}", "Accept": "application/json"},
            None,
        )
        if status != 401:
            return status, body
        self._token = self._refresh(token)
        self._store(self._token)
        return self._raw(
            method,
            url,
            {"Authorization": f"Bearer {self._token.access_token}", "Accept": "application/json"},
            None,
        )

    def _valid_token(self) -> Token:
        token = self._token or self._load()
        if token is None:
            raise NotSignedIn("Not signed in. Run `blynclight-teams login --client-id <application-id>`.")
        if token.expired(self._now()):
            token = self._refresh(token)
            self._store(token)
        self._token = token
        return token

    def _refresh(self, token: Token) -> Token:
        if not token.refresh_token:
            raise NotSignedIn("The saved session has no refresh token. Run login again.")
        payload = self._form(
            self._oauth_url("token"),
            {
                "grant_type": "refresh_token",
                "client_id": self.config.client_id,
                "refresh_token": token.refresh_token,
                "scope": SCOPES,
            },
        )
        error = str(payload.get("error") or "")
        if error:
            description = str(payload.get("error_description") or error)
            raise NotSignedIn(f"Refreshing the Microsoft session failed: {description}")
        refreshed = self._token_from_oauth(payload)
        if not refreshed.refresh_token:
            refreshed.refresh_token = token.refresh_token
        return refreshed

    def _token_from_oauth(self, payload: dict) -> Token:
        expires_in = int(payload.get("expires_in") or 3600)
        return Token(
            access_token=str(payload["access_token"]),
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_at=self._now() + expires_in,
            scope=str(payload.get("scope") or ""),
        )

    def _store(self, token: Token) -> None:
        path = self.config.token_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(token.to_json()))
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)
        self._token = token

    def _load(self) -> Token | None:
        path = self.config.token_path
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise NotSignedIn(f"Cannot read {path}: {exc}") from exc
        try:
            return Token.from_json(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise NotSignedIn(f"{path} is not a saved sign-in. Run login again.") from exc

    def _form(self, url: str, fields: dict[str, str]) -> dict:
        status, body = self._raw(
            "POST",
            url,
            {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            urllib.parse.urlencode(fields).encode(),
        )
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise GraphTemporaryError(f"Microsoft returned HTTP {status} with a non-JSON body") from exc
        if not isinstance(payload, dict):
            raise GraphTemporaryError(f"Microsoft returned HTTP {status} with an unexpected body")
        if status >= 500:
            raise GraphTemporaryError(f"Microsoft returned HTTP {status}: {_snippet(body)}")
        payload["_status"] = status
        return payload

    def _raw(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
        sent = {"User-Agent": USER_AGENT, **headers}
        return self._http(method, url, sent, body)

    def _oauth_url(self, action: str) -> str:
        tenant = urllib.parse.quote(self.config.tenant, safe="")
        return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/{action}"

    @staticmethod
    def _raise_oauth_error(payload: dict) -> None:
        error = payload.get("error")
        if not error:
            return
        description = str(payload.get("error_description") or error)
        raise _signin_error(description)


def tenant_segment(value: str) -> str:
    """Return the directory path segment for a tenant id, domain, or work email."""
    text = value.strip()
    if not text:
        raise ConfigError("Pass a tenant id or a work email.")
    if text.lower() in _TENANT_ALIASES or _TENANT_GUID.fullmatch(text):
        return text
    if "@" in text:
        domain = text.rsplit("@", 1)[1].strip().strip(".")
        if not domain or any(character in domain for character in "@/\\ "):
            raise ConfigError(f"{value!r} is not a work email.")
        return domain
    if "." not in text or any(character in text for character in "/\\ "):
        raise ConfigError(f"{value!r} is not a tenant id, directory domain, or work email.")
    return text


def resolve_tenant(value: str, http: Http = default_http) -> str:
    """Turn a tenant id, domain, or UPN into the directory id stored in config.

    A single-tenant app registration rejects the organizations endpoint
    (AADSTS50059). A work email selects that directory: the domain after @
    is looked up in Entra and replaced with the tenant id.
    """
    segment = tenant_segment(value)
    if segment.lower() in _TENANT_ALIASES or _TENANT_GUID.fullmatch(segment):
        return segment
    quoted = urllib.parse.quote(segment)
    url = f"https://login.microsoftonline.com/{quoted}/v2.0/.well-known/openid-configuration"
    try:
        status, body = http("GET", url, {"Accept": "application/json", "User-Agent": USER_AGENT}, None)
    except GraphTemporaryError as exc:
        raise NotSignedIn(f"Could not look up the directory for {segment}: {exc}") from exc
    try:
        loaded = json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise NotSignedIn(f"Microsoft did not return directory metadata for {segment}.") from exc
    payload = loaded if isinstance(loaded, dict) else {}
    issuer = str(payload.get("issuer") or "")
    match = _ISSUER.fullmatch(issuer)
    if status != 200 or match is None:
        detail = str(payload.get("error_description") or payload.get("error") or "")
        extra = f" {detail}" if detail else ""
        raise NotSignedIn(
            f"Could not find an Entra directory for {segment}.{extra}\n"
            "Pass the tenant id: blynclight-teams login --tenant <tenant-id>"
        )
    return match.group(1)


def _signin_error(description: str) -> NotSignedIn:
    if "AADSTS50059" in description:
        return NotSignedIn(
            "Microsoft could not tell which directory this app belongs to.\n"
            "A single-tenant registration needs your work email or tenant id:\n"
            "  blynclight-teams login --upn you@company.com\n"
            "  blynclight-teams login --tenant <tenant-id>"
        )
    return NotSignedIn(description)


def _snippet(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    if len(text) > 300:
        return text[:300] + "..."
    return text
