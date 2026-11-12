"""Command line: `agentwatch serve`, `agentwatch demo` and `agentwatch hash-password`."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import time
from pathlib import Path

from agentwatch.auth import Auth, AuthConfigError, hash_password
from agentwatch.judges import JudgePolicy, judge_endpoint

GIL_SWITCH_S = 0.0005
DEFAULT_DB = Path(
    os.environ.get("AGENTWATCH_DB", Path.home() / ".agentwatch" / "agentwatch.duckdb")
)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="agentwatch: %(message)s")
    parser = argparse.ArgumentParser(prog="agentwatch", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="receive OTLP traces and serve the UI")
    serve.add_argument(
        "--db", type=Path, default=DEFAULT_DB, help="DuckDB file (default: %(default)s)"
    )
    serve.add_argument("--host", default="127.0.0.1", help="bind address (default: %(default)s)")
    serve.add_argument(
        "--port",
        type=int,
        default=4318,
        help="port for OTLP/HTTP and the UI (default: %(default)s)",
    )
    serve.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="NAME",
        help="also accept requests addressed to this host name (repeatable; '*' for any)",
    )
    serve.add_argument(
        "--public-url",
        default=None,
        metavar="URL",
        help="where people open agentwatch, such as https://traces.example.com; alerts then "
        "carry links to the traces behind them (its host must be served: see --allow-host)",
    )
    serve.add_argument(
        "--retain-days",
        type=int,
        default=None,
        metavar="DAYS",
        help="delete traces that ended more than DAYS ago (checked every 6 hours; default: keep all)",
    )
    serve.add_argument(
        "--memory-limit",
        default=None,
        metavar="SIZE",
        help="cap DuckDB's memory, such as 256MB (default: DuckDB's own, 80%% of RAM)",
    )
    serve.add_argument(
        "--judge-endpoint",
        action="append",
        default=[],
        metavar="URL",
        help="the only base URL judges may call (repeatable; default: any https endpoint)",
    )
    serve.add_argument(
        "--judge-key-env",
        action="append",
        default=[],
        metavar="NAME",
        help="the only API key variable judges may use (repeatable; default: any *_API_KEY)",
    )
    serve.add_argument(
        "--judge-max-daily",
        type=int,
        default=10_000,
        metavar="N",
        help="the highest daily call limit a judge may have (default: %(default)s)",
    )
    serve.add_argument(
        "--auth-user",
        default=None,
        metavar="NAME",
        help="require signing in as NAME, with the password hash in --password-hash-env",
    )
    serve.add_argument(
        "--password-hash-env",
        default="AGENTWATCH_PASSWORD_HASH",
        metavar="VAR",
        help="variable holding the output of `agentwatch hash-password` (default: %(default)s)",
    )
    serve.add_argument(
        "--api-key-env",
        action="append",
        default=[],
        metavar="VAR",
        help="variable holding a key programs may send as 'Authorization: Bearer' (repeatable)",
    )
    serve.add_argument(
        "--insecure-cookies",
        action="store_true",
        help="session cookies without Secure, for plain-HTTP use only (default: Secure)",
    )
    serve.add_argument(
        "--forwarded-allow-ips",
        default=None,
        metavar="IPS",
        help=(
            "addresses or networks of the proxies trusted to name the client in "
            "X-Forwarded-For, such as 172.18.0.1 (avoid '*', which believes any peer)"
        ),
    )
    serve.add_argument(
        "--demo",
        action="store_true",
        help="load the synthetic demo dataset into an in-memory database",
    )

    demo = commands.add_parser("demo", help="write the synthetic demo dataset to a database")
    demo.add_argument("--db", type=Path, default=DEFAULT_DB)
    demo.add_argument("--traces", type=int, default=120)
    demo.add_argument("--seed", type=int, default=7)

    commands.add_parser(
        "hash-password",
        help="read a password and print the hash for --password-hash-env",
    )

    args = parser.parse_args(argv)
    if args.command == "hash-password":
        return _hash_password()
    if args.command == "serve":
        if not 1 <= args.judge_max_daily <= 10_000:
            parser.error("--judge-max-daily must be from 1 to 10000")
        if args.judge_key_env and not args.judge_endpoint:
            # A key pinned without an endpoint could still be sent to any server.
            parser.error("--judge-key-env needs --judge-endpoint")
        try:
            endpoints = tuple(judge_endpoint(u) for u in args.judge_endpoint)
        except ValueError as error:
            parser.error(f"--judge-endpoint: {error}")
        if args.retain_days is not None and not 1 <= args.retain_days <= 36500:
            parser.error("--retain-days must be from 1 to 36500")
        # `is None`, not falsiness: an empty --auth-user (an unset variable in a Compose file)
        # must fail as a bad name, not quietly serve without signing in.
        if args.auth_user is None:
            for flag, given in (
                ("--api-key-env", bool(args.api_key_env)),
                ("--insecure-cookies", args.insecure_cookies),
            ):
                if given:
                    parser.error(f"{flag} needs --auth-user")
            if os.environ.get(args.password_hash_env, "").strip():
                parser.error(
                    f"{args.password_hash_env} is set but --auth-user is not: "
                    "pass --auth-user to require signing in, or unset it"
                )
        auth = None
        if args.auth_user is not None:
            auth = _auth(
                args.auth_user,
                args.password_hash_env,
                args.api_key_env,
                secure_cookies=not args.insecure_cookies,
            )
            if auth is None:
                return 2
        return _serve(
            args.db,
            args.host,
            args.port,
            args.demo,
            args.allow_host,
            retain_days=args.retain_days,
            memory_limit=args.memory_limit,
            judge_policy=JudgePolicy(
                endpoints=endpoints,
                key_envs=tuple(args.judge_key_env),
                max_daily=args.judge_max_daily,
            ),
            auth=auth,
            forwarded_allow_ips=args.forwarded_allow_ips,
            public_url=args.public_url,
        )
    return _demo(args.db, args.traces, args.seed)


def _serve(
    db: Path,
    host: str,
    port: int,
    demo: bool,
    extra_hosts: list[str],
    *,
    retain_days: int | None = None,
    memory_limit: str | None = None,
    judge_policy: JudgePolicy | None = None,
    auth: Auth | None = None,
    forwarded_allow_ips: str | None = None,
    public_url: str | None = None,
) -> int:
    import uvicorn

    from agentwatch import demo as dataset
    from agentwatch.judges import JudgeRunner
    from agentwatch.monitors import MonitorRunner
    from agentwatch.retention import Retention
    from agentwatch.server import LOOPBACK_HOSTS, create_app, public_base
    from agentwatch.store import Store

    allowed = [*LOOPBACK_HOSTS, *extra_hosts]
    if host not in ("0.0.0.0", "::"):
        allowed.append(host)
    if public_url is not None:
        try:
            public_url = public_base(public_url, allowed)
        except ValueError as error:
            print(f"agentwatch: {error}", file=sys.stderr)
            return 2
    try:
        store = Store(None if demo else db, memory_limit=memory_limit)
    except ValueError as error:
        print(f"agentwatch: {error}", file=sys.stderr)
        return 2
    if demo:
        now = time.time_ns()
        store.insert(dataset.generate(end_ns=now), now)
        dataset.seed_datasets(store)
    if host not in LOOPBACK_HOSTS and auth is None:
        print(
            f"warning: binding to {host}; agentwatch has no authentication, "
            "so anyone who can reach this port can read and write traces. "
            "Clients must address it by a name passed with --allow-host",
            file=sys.stderr,
        )
    where = "in-memory demo data" if demo else str(db)
    print(
        f"agentwatch on http://{host}:{port} ({where}); OTLP endpoint http://{host}:{port}/v1/traces"
    )
    # A drift bootstrap is pure Python. At the default 5 ms each step of an ingest request waits
    # that long for the interpreter while one runs, which took ingest from 13 ms to 450 ms.
    sys.setswitchinterval(GIL_SWITCH_S)
    # Judges are disabled until switched on, so the runner makes no model call until then.
    runner = JudgeRunner(store, policy=judge_policy or JudgePolicy())
    runner.start()
    monitor_runner = MonitorRunner(store)
    monitor_runner.start()
    retention = Retention(store, retain_days) if retain_days is not None else None
    if retention is not None:
        retention.start()
    try:
        uvicorn.run(
            create_app(
                store,
                allowed_hosts=allowed,
                judge_runner=runner,
                auth=auth,
                public_url=public_url,
            ),
            host=host,
            port=port,
            log_level="warning",
            # The client address throttles failed sign-ins; behind a proxy it is the proxy's
            # unless the proxy is trusted to name the client.
            proxy_headers=forwarded_allow_ips is not None,
            forwarded_allow_ips=forwarded_allow_ips,
        )
    finally:
        # Within a container's stop grace period: an in-flight judge call, monitor query or
        # prune is left to the daemon threads, and the database is closed only if they have
        # finished (DuckDB replays its log at the next start otherwise).
        runner.stop(timeout_s=3)
        monitor_runner.stop(timeout_s=3)
        if retention is not None:
            retention.stop(timeout_s=3)
        if (
            not runner.running
            and not monitor_runner.running
            and (retention is None or not retention.running)
        ):
            store.close()
    return 0


def _auth(user: str, hash_env: str, key_envs: list[str], *, secure_cookies: bool) -> Auth | None:
    """The sign-in settings from the environment, or None (having said why) if unusable.

    Refusing to start is the safe failure: serving without the sign-in that was asked for
    would expose everything.
    """
    encoded = os.environ.get(hash_env, "")
    if not encoded.strip():
        print(f"agentwatch: --auth-user needs a password hash in {hash_env}", file=sys.stderr)
        return None
    keys: list[str] = []
    for name in key_envs:
        key = os.environ.get(name, "")
        if not key.strip():
            print(f"agentwatch: {name} is empty", file=sys.stderr)
            return None
        keys.append(key)
    try:
        auth = Auth(user, encoded, keys, secure_cookies=secure_cookies)
    except AuthConfigError as error:
        print(f"agentwatch: {error}", file=sys.stderr)
        return None
    print(
        f"agentwatch: signing in required (user {user}, {len(keys)} API key(s))",
        file=sys.stderr,
    )
    return auth


def _hash_password() -> int:
    import getpass

    if sys.stdin.isatty():
        password = getpass.getpass("Password: ")
        if getpass.getpass("Again: ") != password:
            print("agentwatch: the passwords differ", file=sys.stderr)
            return 1
    else:
        password = sys.stdin.readline().rstrip("\r\n")
    try:
        print(hash_password(password))
    except ValueError as error:
        print(f"agentwatch: {error}", file=sys.stderr)
        return 1
    return 0


def _demo(db: Path, traces: int, seed: int) -> int:
    from agentwatch import datasets
    from agentwatch import demo as dataset
    from agentwatch.store import Store

    store = Store(db)
    now = time.time_ns()
    count = store.insert(dataset.generate(traces=traces, seed=seed, end_ns=now), now)
    with contextlib.suppress(datasets.Conflict):  # already seeded
        dataset.seed_datasets(store)
    store.close()
    print(f"wrote {count} synthetic spans in {traces} traces to {db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
