"""Command line: `agentwatch serve` and `agentwatch demo`."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

DEFAULT_DB = Path(
    os.environ.get("AGENTWATCH_DB", Path.home() / ".agentwatch" / "agentwatch.duckdb")
)


def main(argv: list[str] | None = None) -> int:
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
        "--demo",
        action="store_true",
        help="load the synthetic demo dataset into an in-memory database",
    )

    demo = commands.add_parser("demo", help="write the synthetic demo dataset to a database")
    demo.add_argument("--db", type=Path, default=DEFAULT_DB)
    demo.add_argument("--traces", type=int, default=120)
    demo.add_argument("--seed", type=int, default=7)

    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args.db, args.host, args.port, args.demo, args.allow_host)
    return _demo(args.db, args.traces, args.seed)


def _serve(db: Path, host: str, port: int, demo: bool, extra_hosts: list[str]) -> int:
    import uvicorn

    from agentwatch import demo as dataset
    from agentwatch.server import LOOPBACK_HOSTS, create_app
    from agentwatch.store import Store

    store = Store(None if demo else db)
    if demo:
        now = time.time_ns()
        store.insert(dataset.generate(end_ns=now), now)
    allowed = [*LOOPBACK_HOSTS, *extra_hosts]
    if host not in ("0.0.0.0", "::"):
        allowed.append(host)
    if host not in LOOPBACK_HOSTS:
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
    try:
        uvicorn.run(
            create_app(store, allowed_hosts=allowed), host=host, port=port, log_level="warning"
        )
    finally:
        store.close()
    return 0


def _demo(db: Path, traces: int, seed: int) -> int:
    from agentwatch import demo as dataset
    from agentwatch.store import Store

    store = Store(db)
    now = time.time_ns()
    count = store.insert(dataset.generate(traces=traces, seed=seed, end_ns=now), now)
    store.close()
    print(f"wrote {count} synthetic spans in {traces} traces to {db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
