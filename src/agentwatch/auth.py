"""Sign-in for a server reachable beyond loopback: one account, sessions, and API keys.

Off unless `agentwatch serve --auth-user` is given. The design follows OWASP's password storage
and session management cheat sheets and the Auth Book (auth.pilcrowonpaper.com), on the
standard library's primitives, not new cryptography:

- The password is stored only as a scrypt hash (N=2^15, r=8, p=3: one of OWASP's equivalent
  minimums, 32 MiB a check), made by `agentwatch hash-password`, and checked in constant time.
  An unknown user name costs the same check, so timing does not tell names apart.
- A session is a random 256-bit token in an HttpOnly, Secure, SameSite=Lax `__Host-` cookie.
  The server keeps only its SHA-256, in memory, so signing out ends it at once and a restart
  ends them all. It lasts 7 days unused and 30 days at most.
- Failed sign-ins are throttled per client address (5 free, then a wait that doubles up to an
  hour) and for the account (at most 30 an hour from everyone), so guessing is slow and one
  address cannot lock the owner out.
- Programs (an OTLP exporter, an alert relay) send `Authorization: Bearer <key>` instead, with
  keys the operator puts in environment variables.

Cross-site writes are refused by the server's host guard and JSON-only writes, as before; the
cookie's SameSite adds to that, it does not replace it.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import html
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs

import anyio
import anyio.to_thread
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("agentwatch")

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 3
# Bounds on a stored hash's parameters, so a mistyped one cannot exhaust a small container:
# scrypt needs 128 * r * N bytes.
MAX_SCRYPT_MEMORY = 64 * 1024 * 1024
MAX_SCRYPT_P = 16
SALT_BYTES = 16
KEY_BYTES = 32
MIN_PASSWORD = 12
MAX_PASSWORD = 1024

SESSION_IDLE_S = 7 * 24 * 3600
SESSION_MAX_S = 30 * 24 * 3600
MAX_SESSIONS = 64
SECURE_COOKIE = "__Host-agentwatch"
# Without Secure the __Host- prefix is refused by browsers, so plain HTTP gets another name.
PLAIN_COOKIE = "agentwatch_session"

FREE_FAILURES = 5
MAX_WAIT_S = 3600
ACCOUNT_FAILURES_PER_HOUR = 30
# Password checks waiting their turn beyond this get 503: a burst must not queue without bound.
MAX_WAITING_CHECKS = 8
MAX_CLIENTS = 10_000
MAX_FORM_BYTES = 8 * 1024

USER_NAME = re.compile(r"[A-Za-z0-9._@-]{1,64}")
# Where to go after signing in: a UI route (the part after #), nothing else.
NEXT_ROUTE = re.compile(r"#/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]{0,512}")


class AuthConfigError(ValueError):
    """The sign-in settings cannot be used."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=n,
        r=r,
        p=p,
        # 128 * r * n bytes, plus room; hashlib refuses anything above 32 MiB by default.
        maxmem=256 * r * n + 1024 * 1024,
        dklen=KEY_BYTES,
    )


def hash_password(password: str) -> str:
    """`scrypt:N:r:p:salt:key`, base64url without padding: no `$`, so .env files keep it whole."""
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise ValueError(f"a password must be {MIN_PASSWORD} to {MAX_PASSWORD} characters")
    salt = secrets.token_bytes(SALT_BYTES)
    key = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}:{_b64(salt)}:{_b64(key)}"


@dataclass(frozen=True)
class PasswordHash:
    n: int
    r: int
    p: int
    salt: bytes
    key: bytes

    @classmethod
    def parse(cls, encoded: str) -> PasswordHash:
        parts = encoded.strip().split(":")
        if len(parts) != 6 or parts[0] != "scrypt":
            raise AuthConfigError("not a password hash from `agentwatch hash-password`")
        try:
            n, r, p = (int(x) for x in parts[1:4])
            salt, key = _unb64(parts[4]), _unb64(parts[5])
        except ValueError as error:
            raise AuthConfigError("the password hash is damaged") from error
        if (
            n < 2**14
            or n & (n - 1)
            or not 1 <= r <= 32
            or 128 * r * n > MAX_SCRYPT_MEMORY
            or not 1 <= p <= MAX_SCRYPT_P
            or len(salt) < 16
            or len(key) != KEY_BYTES
        ):
            raise AuthConfigError("the password hash has unusable parameters")
        return cls(n, r, p, salt, key)

    def matches(self, password: str) -> bool:
        if len(password) > MAX_PASSWORD:
            password = ""
        found = _scrypt(password, self.salt, self.n, self.r, self.p)
        return hmac.compare_digest(found, self.key)


def _digest(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


@dataclass
class _Session:
    user: str
    created: float
    seen: float


class Sessions:
    """Signed-in sessions, by the SHA-256 of their token; kept in memory only."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: OrderedDict[bytes, _Session] = OrderedDict()

    def create(self, user: str) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._sessions[_digest(token)] = _Session(user, now, now)
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)
        return token

    def user(self, token: str) -> str | None:
        if not token or len(token) > 128:
            return None
        key = _digest(token)
        now = self._clock()
        with self._lock:
            found = self._sessions.get(key)
            if found is None:
                return None
            if now - found.seen > SESSION_IDLE_S or now - found.created > SESSION_MAX_S:
                del self._sessions[key]
                return None
            found.seen = now
            self._sessions.move_to_end(key)
            return found.user

    def end(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(_digest(token), None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


@dataclass
class _Client:
    failures: int = 0
    blocked_until: float = 0.0


class Throttle:
    """Failed sign-ins: a doubling wait per client address, and an hourly cap for the account."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._clients: OrderedDict[str, _Client] = OrderedDict()
        self._account: list[float] = []

    def _wait(self, client: str, now: float) -> int:
        self._account = [t for t in self._account if now - t < 3600]
        waits = [0.0]
        found = self._clients.get(client)
        if found is not None:
            waits.append(found.blocked_until - now)
        if len(self._account) >= ACCOUNT_FAILURES_PER_HOUR:
            waits.append(3600 - (now - self._account[0]))
        return max(0, int(max(waits) + 0.999))

    def wait(self, client: str) -> int:
        """Seconds this client must wait before trying again; 0 when it may try now."""
        with self._lock:
            return self._wait(client, self.clock())

    def begin(self, client: str) -> tuple[int, float | None]:
        """Takes an attempt: `(0, ticket)` when the client may try now, `(seconds, None)` when
        it must wait. The attempt counts as a failure, for the client and the account, from
        this moment until `succeeded` gives it back, so attempts still being checked count
        against the limits and a burst of them cannot slip through together."""
        now = self.clock()
        with self._lock:
            wait = self._wait(client, now)
            if wait:
                return wait, None
            self._account.append(now)
            found = self._clients.pop(client, None) or _Client()
            found.failures += 1
            if found.failures >= FREE_FAILURES:
                wait_s = min(MAX_WAIT_S, 60 * 2 ** (found.failures - FREE_FAILURES))
                found.blocked_until = now + wait_s
            self._clients[client] = found
            while len(self._clients) > MAX_CLIENTS:
                self._clients.popitem(last=False)
            return 0, now

    def succeeded(self, client: str, ticket: float) -> None:
        """The attempt was right: clears the client's record and gives the account its try back."""
        with self._lock:
            self._clients.pop(client, None)
            with contextlib.suppress(ValueError):
                self._account.remove(ticket)


class Auth:
    """The server's sign-in: one account from the operator, plus API keys for programs."""

    def __init__(
        self,
        user: str,
        password_hash: str,
        api_keys: Sequence[str] = (),
        *,
        secure_cookies: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not USER_NAME.fullmatch(user):
            raise AuthConfigError("a user name is 1 to 64 letters, digits or ._@-")
        self.user = user
        self.password = PasswordHash.parse(password_hash)
        keys = [k.strip() for k in api_keys]
        if any(len(k) < 32 for k in keys):
            raise AuthConfigError("an API key must be at least 32 characters")
        self._keys = [_digest(k) for k in keys]
        self.secure_cookies = secure_cookies
        self.cookie = SECURE_COOKIE if secure_cookies else PLAIN_COOKIE
        self.sessions = Sessions(clock)
        self.throttle = Throttle(clock)
        # One check at a time, on its own thread (checking_limiter), not the pool the API and
        # ingest share: 32 MiB each, and a queue of guesses gains nothing by running wide.
        self._limiter: anyio.CapacityLimiter | None = None
        self.waiting_checks = 0
        # Checked against for an unknown name, so it takes as long as a known one.
        self._decoy = PasswordHash(
            self.password.n,
            self.password.r,
            self.password.p,
            secrets.token_bytes(SALT_BYTES),
            secrets.token_bytes(KEY_BYTES),
        )

    def api_key_valid(self, presented: str) -> bool:
        digest = _digest(presented)
        # Every key is compared, so the time taken does not say which one nearly matched.
        return bool(sum(hmac.compare_digest(digest, k) for k in self._keys))

    def check(self, user: str, password: str) -> bool:
        known = hmac.compare_digest(_digest(user), _digest(self.user))
        matched = (self.password if known else self._decoy).matches(password)
        return known and matched

    async def check_in_turn(self, user: str, password: str) -> bool:
        if self._limiter is None:
            # Made here, inside the event loop, which anyio needs.
            self._limiter = anyio.CapacityLimiter(1)
        return await anyio.to_thread.run_sync(self.check, user, password, limiter=self._limiter)

    def requester(self, scope: Scope) -> str | None:
        """Who sent this request: the signed-in user, `api-key`, or None."""
        headers = Headers(scope=scope)
        authorization = headers.get("authorization", "")
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() == "bearer" and credential:
            return "api-key" if self.api_key_valid(credential.strip()) else None
        return self.sessions.user(_cookie(headers, self.cookie))

    def set_cookie(self, response: Response, token: str) -> None:
        response.set_cookie(
            self.cookie,
            token,
            max_age=SESSION_MAX_S,
            path="/",
            secure=self.secure_cookies,
            httponly=True,
            samesite="lax",
        )

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(
            self.cookie, path="/", secure=self.secure_cookies, httponly=True, samesite="lax"
        )


def _cookie(headers: Headers, name: str) -> str:
    for part in headers.get("cookie", "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


# Reachable without signing in. Everything else needs a session or an API key.
# Signing out is open too, so a stale cookie can still be cleared.
PUBLIC = frozenset(
    {"/login", "/api/auth/login", "/api/auth/logout", "/api/auth/session", "/healthz"}
)


class AuthGuard:
    """Requests without a session or API key: the UI page goes to the sign-in page, the API
    and OTLP ingest get 401."""

    def __init__(self, app: ASGIApp, auth: Auth) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        requester = self.auth.requester(scope)
        scope.setdefault("state", {})["requester"] = requester
        if requester is not None or path in PUBLIC:
            await self.app(scope, receive, send)
            return
        if scope["method"] in ("GET", "HEAD") and path in ("/", "/index.html"):
            # A browser keeps the URL's #route across the redirect, so it comes back after.
            response: Response = Response(
                status_code=303, headers={"location": "/login", "cache-control": "no-store"}
            )
        else:
            response = PlainTextResponse(
                "sign in or send an API key",
                status_code=401,
                headers={"www-authenticate": 'Bearer realm="agentwatch"'},
            )
        await response(scope, receive, send)


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def login_page(request: Request) -> Response:
    return _page()


async def login(auth: Auth, request: Request) -> Response:
    """A sign-in form post: a session cookie and a redirect to the UI, or the form again."""
    client = client_address(request)
    wait = auth.throttle.wait(client)
    if wait:
        return _page("Too many attempts. Try again later.", 429, {"retry-after": str(wait)})
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media != "application/x-www-form-urlencoded":
        return PlainTextResponse("send the sign-in form", status_code=415)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            return PlainTextResponse("request too large", status_code=413)
    try:
        form = parse_qs(body.decode(), max_num_fields=8)
    except (UnicodeDecodeError, ValueError):
        return PlainTextResponse("send the sign-in form", status_code=400)
    user = (form.get("username") or [""])[0].strip()
    password = (form.get("password") or [""])[0]
    following = (form.get("next") or [""])[0]
    if auth.waiting_checks >= MAX_WAITING_CHECKS:
        return _page("Busy. Try again in a moment.", 503, {"retry-after": "5"})
    wait, ticket = auth.throttle.begin(client)
    if ticket is None:
        return _page("Too many attempts. Try again later.", 429, {"retry-after": str(wait)})
    auth.waiting_checks += 1
    try:
        matched = await auth.check_in_turn(user, password)
    finally:
        auth.waiting_checks -= 1
    if matched:
        auth.throttle.succeeded(client, ticket)
        old = _cookie(request.headers, auth.cookie)
        if old:
            auth.sessions.end(old)
        log.info("sign-in succeeded for %s", auth.user)
        response = Response(
            status_code=303,
            headers={
                "location": "/" + (following if NEXT_ROUTE.fullmatch(following) else ""),
                "cache-control": "no-store",
            },
        )
        auth.set_cookie(response, auth.sessions.create(auth.user))
        return response
    # The address only; never the name or password tried.
    log.warning("sign-in failed from %s", client)
    return _page("Wrong user name or password.", 401)


async def logout(auth: Auth, request: Request) -> Response:
    # A write like any other: JSON only, so a cross-site form cannot sign the owner out.
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        return PlainTextResponse("send application/json", status_code=415)
    token = _cookie(request.headers, auth.cookie)
    if token:
        auth.sessions.end(token)
    response = Response(status_code=204)
    auth.clear_cookie(response)
    return response


def session_json(auth: Auth | None, request: Request) -> Response:
    requester = request.scope.get("state", {}).get("requester")
    return JSONResponse(
        {"auth": auth is not None, "user": requester if requester != "api-key" else None},
        headers={"cache-control": "no-store"},
    )


# Copies the page's #route into the form, so signing in returns to it. Optional: without
# script the form still works and lands on the trace list.
_SCRIPT = 'document.getElementById("next").value=location.hash.startsWith("#/")?location.hash:"";'
_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_SCRIPT.encode()).digest()).decode()
_STYLE = """
:root{color-scheme:light dark;--bg:#fbfbfa;--panel:#fff;--ink:#1d2125;--muted:#69707a;
--line:#e4e6e9;--accent:#2f6fdb;--on-accent:#fff;--bad:#a3261f}
@media (prefers-color-scheme:dark){:root{--bg:#15171a;--panel:#1c1f23;--ink:#e6e8eb;
--muted:#9aa1ab;--line:#2c3036;--accent:#7ea8f5;--on-accent:#15171a;--bad:#f0928b}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;
background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,sans-serif;
padding:16px}form{width:100%;max-width:340px;background:var(--panel);border:1px solid
var(--line);border-radius:10px;padding:24px;display:grid;gap:14px}h1{margin:0;font-size:20px}
label{display:grid;gap:4px;font-size:14px;color:var(--muted)}input{font:inherit;color:inherit;
background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:9px 10px}
input:focus{outline:2px solid var(--accent);outline-offset:1px}button{font:inherit;
font-weight:600;color:var(--on-accent);background:var(--accent);border:0;border-radius:6px;padding:10px;
cursor:pointer}p{margin:0;color:var(--bad);font-size:14px}
"""


def _page(error: str = "", status: int = 200, headers: dict[str, str] | None = None) -> Response:
    message = f'<p role="alert">{html.escape(error)}</p>' if error else ""
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · agentwatch</title><style>{_STYLE}</style></head>
<body><form method="post" action="/api/auth/login">
<h1>agentwatch</h1>{message}
<label>User name<input name="username" autocomplete="username" autocapitalize="none"
 spellcheck="false" required maxlength="64" autofocus></label>
<label>Password<input name="password" type="password" autocomplete="current-password"
 required maxlength="{MAX_PASSWORD}"></label>
<input type="hidden" name="next" id="next">
<button type="submit">Sign in</button>
</form><script>{_SCRIPT}</script></body></html>"""
    return HTMLResponse(
        body,
        status_code=status,
        headers={
            "cache-control": "no-store",
            "content-security-policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                f"script-src 'sha256-{_SCRIPT_HASH}'; form-action 'self'; "
                "frame-ancestors 'none'; base-uri 'none'"
            ),
            "x-frame-options": "DENY",
            # Not no-referrer: under it browsers send `Origin: null` with the form, which the
            # host guard refuses as cross-site.
            "referrer-policy": "same-origin",
            **(headers or {}),
        },
    )
