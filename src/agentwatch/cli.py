"""Command line: `agentwatch serve` and `agentwatch demo`."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import time
from pathlib import Path

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
        if args.retain_days is not None and not 1 <= args.retain_days <= 36500:
            parser.error("--retain-days must be from 1 to 36500")
        return _serve(
            args.db,
            args.host,
            args.port,
            args.demo,
            args.allow_host,
            retain_days=args.retain_days,
            memory_limit=args.memory_limit,
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
) -> int:
    import uvicorn

    from agentwatch import demo as dataset
    from agentwatch.judges import JudgeRunner
    from agentwatch.retention import Retention
    from agentwatch.server import LOOPBACK_HOSTS, create_app
    from agentwatch.store import Store

    try:
        store = Store(None if demo else db, memory_limit=memory_limit)
    except ValueError as error:
        print(f"agentwatch: {error}", file=sys.stderr)
        return 2
    if demo:
        now = time.time_ns()
        store.insert(dataset.generate(end_ns=now), now)
        dataset.seed_datasets(store)
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
    # Judges are disabled until switched on, so the runner makes no model call until then.
    runner = JudgeRunner(store)
    runner.start()
    retention = Retention(store, retain_days) if retain_days is not None else None
    if retention is not None:
        retention.start()
    try:
        uvicorn.run(
            create_app(store, allowed_hosts=allowed, judge_runner=runner),
            host=host,
            port=port,
            log_level="warning",
        )
    finally:
        # Within a container's stop grace period: an in-flight judge call or prune is left
        # to the daemon threads, and the database is closed only if they have finished
        # (DuckDB replays its log at the next start otherwise).
        runner.stop(timeout_s=3)
        if retention is not None:
            retention.stop(timeout_s=3)
        if not runner.running and (retention is None or not retention.running):
            store.close()
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
