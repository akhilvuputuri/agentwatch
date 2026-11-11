from __future__ import annotations

import io
import logging
import time

import anyio
import httpx2
import pytest
from starlette.testclient import TestClient

from agentwatch import auth, cli, server
from agentwatch.store import Store
from tests.builders import encode, to_otlp_json
from tests.test_server import spans

PASSWORD = "correct horse battery staple"
# Made once: each hash costs a 32 MiB scrypt.
HASH = auth.hash_password(PASSWORD)
KEY = "k" * 40


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def app(clock: Clock | None = None, **options: object) -> tuple[TestClient, auth.Auth]:
    signin = auth.Auth("owner", HASH, [KEY], clock=clock or Clock(), **options)  # type: ignore[arg-type]
    http = TestClient(
        server.create_app(Store(), allowed_hosts=("testserver",), auth=signin),
        base_url="https://testserver",
        follow_redirects=False,
    )
    return http, signin


def sign_in(http: TestClient, password: str = PASSWORD, **extra: str):
    return http.post("/api/auth/login", data={"username": "owner", "password": password, **extra})


def test_a_password_hash_round_trips_and_keeps_out_of_env_syntax() -> None:
    assert HASH.startswith("scrypt:32768:8:3:")
    assert "$" not in HASH
    parsed = auth.PasswordHash.parse(HASH)
    assert parsed.matches(PASSWORD)
    assert not parsed.matches(PASSWORD + "!")
    # A fresh salt each time.
    assert auth.hash_password(PASSWORD) != HASH
    with pytest.raises(ValueError, match="12 to 1024 characters"):
        auth.hash_password("short")


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "bcrypt:1:2:3:4:5",
        "scrypt:1024:8:1:" + HASH.split(":", 4)[4],  # too weak
        "scrypt:1073741824:8:1:" + HASH.split(":", 4)[4],  # too costly
        "scrypt:131072:32:1:" + HASH.split(":", 4)[4],  # 512 MiB: each bound alone allows it
        "scrypt:32768:8:3:AAAA:" + HASH.rsplit(":", 1)[1],  # salt too short
        HASH[:-4],  # key cut short
    ],
)
def test_unusable_hashes_are_refused_at_start(encoded: str) -> None:
    with pytest.raises(auth.AuthConfigError):
        auth.Auth("owner", encoded)


def test_short_api_keys_and_odd_user_names_are_refused() -> None:
    with pytest.raises(auth.AuthConfigError):
        auth.Auth("owner", HASH, ["short"])
    with pytest.raises(auth.AuthConfigError):
        auth.Auth("own er", HASH)


def test_without_signing_in_only_the_sign_in_page_and_health_answer() -> None:
    http, _ = app()
    page = http.get("/")
    assert page.status_code == 303
    assert page.headers["location"] == "/login"
    for path in ("/api/traces", "/api/memory", "/api/alerts", "/assets/x.js", "/login/"):
        assert http.get(path).status_code == 401, path
    for method in ("HEAD", "OPTIONS"):
        assert http.request(method, "/api/traces").status_code == 401, method
    assert http.get("/index.html").status_code == 303
    assert http.post("/v1/traces", json=to_otlp_json(encode(spans()))).status_code == 401
    assert http.post("/api/scores", json={}).status_code == 401
    # Health says nothing about the data.
    assert http.get("/healthz").json() == {"ok": True}
    assert http.get("/api/auth/session").json() == {"auth": True, "user": None}
    login = http.get("/login")
    assert login.status_code == 200
    assert "frame-ancestors 'none'" in login.headers["content-security-policy"]
    assert 'autocomplete="current-password"' in login.text
    assert login.headers["referrer-policy"] == "same-origin"


def test_signing_in_sets_a_host_only_secure_cookie_and_opens_the_api() -> None:
    http, signin = app()
    response = sign_in(http)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-agentwatch=")
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert attribute.lower() in cookie.lower(), attribute
    assert "domain" not in cookie.lower()
    assert http.get("/api/traces").status_code == 200
    assert http.get("/api/auth/session").json() == {"auth": True, "user": "owner"}
    assert len(signin.sessions) == 1


def test_signing_in_returns_to_the_route_and_ignores_other_destinations() -> None:
    http, _ = app()
    assert sign_in(http, next="#/trace/" + "a" * 32).headers["location"] == "/#/trace/" + "a" * 32
    for elsewhere in ("//evil.example", "https://evil.example", "/\\evil", "#/x\nLocation: y"):
        assert sign_in(http, next=elsewhere).headers["location"] == "/", elsewhere


def test_a_wrong_password_or_name_gets_one_answer() -> None:
    http, _ = app()
    wrong = sign_in(http, "not the password at all")
    unknown = http.post("/api/auth/login", data={"username": "root", "password": PASSWORD})
    for response in (wrong, unknown):
        assert response.status_code == 401
        assert "Wrong user name or password." in response.text
        assert "set-cookie" not in response.headers
    assert http.get("/api/traces").status_code == 401


def test_the_sign_in_form_must_be_a_form_and_from_this_site() -> None:
    http, _ = app()
    as_json = http.post("/api/auth/login", json={"username": "owner", "password": PASSWORD})
    assert as_json.status_code == 415
    cross_site = http.post(
        "/api/auth/login",
        data={"username": "owner", "password": PASSWORD},
        headers={"origin": "https://evil.example"},
    )
    assert cross_site.status_code == 403


def test_signing_out_clears_a_stale_cookie_too() -> None:
    http, _ = app()
    http.cookies.set("__Host-agentwatch", "expired-or-unknown", domain="testserver")
    response = http.post("/api/auth/logout", json={})
    assert response.status_code == 204
    assert "__Host-agentwatch=" in response.headers["set-cookie"]


def test_signing_out_ends_the_session_on_the_server() -> None:
    http, signin = app()
    token = sign_in(http).cookies["__Host-agentwatch"]
    # A cross-site form cannot sign the owner out: logout is a JSON write.
    assert http.post("/api/auth/logout", data={}).status_code == 415
    response = http.post("/api/auth/logout", json={})
    assert response.status_code == 204
    assert len(signin.sessions) == 0
    # The old token no longer works, even if a copy of the cookie survived.
    http.cookies.set("__Host-agentwatch", token, domain="testserver")
    assert http.get("/api/traces").status_code == 401


def test_signing_in_again_replaces_the_session_it_came_with() -> None:
    http, signin = app()
    sign_in(http)
    sign_in(http)
    assert len(signin.sessions) == 1


def test_sessions_end_after_a_week_unused_or_a_month_in_all() -> None:
    clock = Clock()
    sessions = auth.Sessions(clock)
    idle = sessions.create("owner")
    clock.now += auth.SESSION_IDLE_S + 1
    assert sessions.user(idle) is None
    busy = sessions.create("owner")
    # Used every 7 days, it lasts 28 days; three days later it is past 30 in all.
    for _ in range(4):
        clock.now += auth.SESSION_IDLE_S - 60
        assert sessions.user(busy) == "owner"
    clock.now += 3 * 24 * 3600
    assert sessions.user(busy) is None
    assert sessions.user("") is None
    assert sessions.user("x" * 500) is None


def test_the_least_recently_used_session_makes_way() -> None:
    sessions = auth.Sessions(Clock())
    first = sessions.create("owner")
    second = sessions.create("owner")
    # Using the first moves it to the back of the line, so the second goes.
    assert sessions.user(first) == "owner"
    for _ in range(auth.MAX_SESSIONS - 1):
        sessions.create("owner")
    assert sessions.user(second) is None
    assert sessions.user(first) == "owner"
    assert len(sessions) == auth.MAX_SESSIONS


def test_an_api_key_opens_ingest_and_the_api_without_a_session() -> None:
    http, _ = app()
    bearer = {"authorization": f"Bearer {KEY}"}
    assert (
        http.post("/v1/traces", json=to_otlp_json(encode(spans())), headers=bearer).status_code
        == 200
    )
    assert http.get("/api/alerts", headers=bearer).status_code == 200
    assert (
        http.get("/api/traces", headers={"authorization": "Bearer " + "x" * 40}).status_code == 401
    )
    # A key is not a user.
    assert http.get("/api/auth/session", headers=bearer).json() == {"auth": True, "user": None}


def test_failed_sign_ins_wait_longer_and_longer_per_address() -> None:
    clock = Clock()
    http, signin = app(clock)
    for _ in range(auth.FREE_FAILURES):
        assert sign_in(http, "wrong password!").status_code == 401
    blocked = sign_in(http)
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"
    # Another address may still try.
    assert signin.throttle.wait("192.0.2.9") == 0
    clock.now += 61
    assert sign_in(http, "wrong password!").status_code == 401
    assert sign_in(http).headers["retry-after"] == "120"
    clock.now += 121
    assert sign_in(http).status_code == 303
    # Success clears the address's record.
    assert signin.throttle.wait("testclient") == 0


def test_the_wait_stops_at_an_hour() -> None:
    clock = Clock()
    throttle = auth.Throttle(clock)
    for _ in range(40):
        assert throttle.begin("198.51.100.1")[1] is not None
        clock.now += 3601
    throttle.begin("198.51.100.1")
    assert throttle.wait("198.51.100.1") == auth.MAX_WAIT_S


def test_the_throttle_counts_thirty_failures_an_hour_for_the_account() -> None:
    clock = Clock()
    throttle = auth.Throttle(clock)
    for i in range(auth.ACCOUNT_FAILURES_PER_HOUR):
        assert throttle.begin(f"203.0.113.{i}")[1] is not None
        clock.now += 1
    assert throttle.wait("192.0.2.1") > 3500
    assert throttle.begin("192.0.2.1") == (throttle.wait("192.0.2.1"), None)
    clock.now += 3600
    assert throttle.wait("192.0.2.1") == 0


def test_a_right_password_gives_its_attempt_back() -> None:
    throttle = auth.Throttle(Clock())
    for _ in range(auth.ACCOUNT_FAILURES_PER_HOUR * 2):
        _, ticket = throttle.begin("192.0.2.1")
        assert ticket is not None
        throttle.succeeded("192.0.2.1", ticket)
    assert throttle.wait("192.0.2.7") == 0


def burst(signin: auth.Auth, requests: int, *, clients: int = 1) -> list[int]:
    """Wrong passwords sent at once, through the app, from `clients` addresses in turn."""
    app = server.create_app(Store(), allowed_hosts=("testserver",), auth=signin)

    async def run() -> list[int]:
        statuses: list[int] = []

        async def one(i: int) -> None:
            transport = httpx2.ASGITransport(app=app, client=(f"192.0.2.{i % clients}", 1000))
            async with httpx2.AsyncClient(
                transport=transport, base_url="https://testserver"
            ) as http:
                response = await http.post(
                    "/api/auth/login", data={"username": "owner", "password": "wrong guess!!"}
                )
                statuses.append(response.status_code)

        async with anyio.create_task_group() as group:
            for i in range(requests):
                group.start_soon(one, i)
        return statuses

    return anyio.run(run)


def test_a_burst_from_one_address_gets_only_the_free_tries() -> None:
    _, signin = app()
    statuses = burst(signin, 20)
    assert statuses.count(401) == auth.FREE_FAILURES
    assert statuses.count(429) == 20 - auth.FREE_FAILURES


def test_a_burst_from_many_addresses_stops_at_the_account_cap_and_queue() -> None:
    _, signin = app()
    statuses = burst(signin, 60, clients=60)
    # Each check waits its turn; beyond the queue's length the rest are told to come back.
    assert statuses.count(401) <= auth.ACCOUNT_FAILURES_PER_HOUR
    assert set(statuses) <= {401, 429, 503}
    assert signin.waiting_checks == 0


def test_the_api_stays_quick_while_sign_ins_are_checked() -> None:
    _, signin = app()
    application = server.create_app(Store(), allowed_hosts=("testserver",), auth=signin)

    def client(address: str) -> httpx2.AsyncClient:
        transport = httpx2.ASGITransport(app=application, client=(address, 1000))
        return httpx2.AsyncClient(transport=transport, base_url="https://testserver")

    async def run() -> float:
        elapsed = float("inf")

        async def guess(i: int) -> None:
            async with client(f"198.51.100.{i}") as http:
                await http.post(
                    "/api/auth/login", data={"username": "owner", "password": "wrong guess!!"}
                )

        async with anyio.create_task_group() as group:
            # More guesses than the shared thread pool's 40, from addresses the per-address
            # wait does not stop. The account cap, the queue limit and the checks' own
            # limiter each keep them from holding the threads the API needs.
            for i in range(60):
                group.start_soon(guess, i)
            await anyio.sleep(0.05)
            started = time.perf_counter()
            async with client("192.0.2.200") as http:
                response = await http.get("/api/traces", headers={"authorization": f"Bearer {KEY}"})
            assert response.status_code == 200
            elapsed = time.perf_counter() - started
        return elapsed

    assert anyio.run(run) < 0.5


def test_failures_are_logged_without_what_was_tried(caplog: pytest.LogCaptureFixture) -> None:
    http, _ = app()
    with caplog.at_level(logging.INFO, logger="agentwatch"):
        sign_in(http, "a guess at the password")
        http.post("/api/auth/login", data={"username": "mallory", "password": "x" * 12})
    text = caplog.text
    assert "sign-in failed from testclient" in text
    assert "guess" not in text
    assert "mallory" not in text


def test_plain_http_cookies_drop_the_host_prefix() -> None:
    http, _ = app(secure_cookies=False)
    cookie = sign_in(http).headers["set-cookie"]
    assert cookie.startswith("agentwatch_session=")
    assert "secure" not in cookie.lower()


def test_with_signing_in_off_nothing_changes() -> None:
    http = TestClient(server.create_app(Store(), allowed_hosts=("testserver",)))
    assert http.get("/api/traces").status_code == 200
    assert http.get("/api/auth/session").json() == {"auth": False, "user": None}
    assert "spans" in http.get("/healthz").json()
    assert http.post("/api/auth/login", data={}).status_code in (404, 405)


def test_the_cli_refuses_to_serve_without_the_hash_it_was_told_to_use(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AGENTWATCH_PASSWORD_HASH", raising=False)
    assert cli.main(["serve", "--demo", "--auth-user", "owner"]) == 2
    assert "needs a password hash" in capsys.readouterr().err
    # An empty name, as from an unset Compose variable, fails rather than serving openly.
    monkeypatch.setenv("AGENTWATCH_PASSWORD_HASH", HASH)
    assert cli.main(["serve", "--demo", "--auth-user", ""]) == 2
    assert "user name" in capsys.readouterr().err
    # A hash without --auth-user is a forgotten flag, not a choice to serve openly.
    with pytest.raises(SystemExit):
        cli.main(["serve", "--demo"])
    assert "--auth-user is not" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["serve", "--demo", "--insecure-cookies"])
    monkeypatch.delenv("AGENTWATCH_PASSWORD_HASH")
    assert cli.main(["serve", "--demo", "--auth-user", "owner"]) == 2
    assert "needs a password hash in AGENTWATCH_PASSWORD_HASH" in capsys.readouterr().err
    monkeypatch.setenv("AGENTWATCH_PASSWORD_HASH", HASH)
    monkeypatch.setenv("AW_KEY", "")
    assert cli.main(["serve", "--demo", "--auth-user", "owner", "--api-key-env", "AW_KEY"]) == 2
    assert "AW_KEY is empty" in capsys.readouterr().err
    monkeypatch.delenv("AGENTWATCH_PASSWORD_HASH")
    with pytest.raises(SystemExit):
        cli.main(["serve", "--demo", "--api-key-env", "AW_KEY"])


def test_hash_password_reads_stdin_when_piped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert cli.main(["hash-password"]) == 0
    printed = capsys.readouterr().out.strip()
    assert auth.PasswordHash.parse(printed).matches(PASSWORD)
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert cli.main(["hash-password"]) == 1
