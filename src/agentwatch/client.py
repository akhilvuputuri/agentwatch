"""A small client for a running agentwatch server, and an experiment runner.

    from agentwatch.client import run_experiment

    def answer(input):            # the task under test
        return my_agent(input)

    def exact(input, output, expected):
        return output == expected  # a boolean score named "exact"

    result = run_experiment(dataset="Demo requests", name="prompt v2", task=answer,
                            evaluators=[exact])
    print(result.scores)

Only the standard library is used, so the runner works inside any project.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

DEFAULT_URL = "http://127.0.0.1:4318"

Task = Callable[[Any], Any]
# An evaluator gets (input, output, expected_output) and returns a number, a boolean, a label
# (str), a score dict ({"name", "value"/"label", "comment"}), a list of those, or None.
Evaluator = Callable[[Any, Any, Any], Any]


class AgentwatchError(RuntimeError):
    """The server refused a request; the message is its reply."""


class Client:
    def __init__(self, base_url: str = DEFAULT_URL, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body, allow_nan=False).encode()
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={"content-type": "application/json"} if data is not None else {},
        )
        url = f"{self.base_url}{path}"
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            raise AgentwatchError(
                f"{error.code}: {error.read().decode(errors='replace').strip()}"
            ) from error
        except (urllib.error.URLError, OSError) as error:
            raise AgentwatchError(f"could not reach agentwatch at {url}: {error}") from error
        try:
            return json.loads(payload) if payload else None
        except ValueError as error:
            raise AgentwatchError(f"{url} did not answer with JSON; is base_url right?") from error

    def dataset(self, name_or_id: str) -> dict[str, Any]:
        """A dataset by ID, or by name when no dataset has that ID."""
        for dataset in self.request("GET", "/api/datasets")["datasets"]:
            if name_or_id in (dataset["id"], dataset["name"]):
                return dataset
        raise AgentwatchError(f"no dataset named or with ID {name_or_id!r}")

    def items(self, dataset_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/datasets/{urllib.parse.quote(dataset_id)}/items")["items"]

    def create_run(
        self,
        dataset_id: str,
        name: str,
        *,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = {"name": name, "description": description, "metadata": metadata or {}}
        return self.request("POST", f"/api/datasets/{urllib.parse.quote(dataset_id)}/runs", body)[
            "run"
        ]

    def add_run_item(self, run_id: str, item: dict[str, Any]) -> None:
        self.request("POST", f"/api/runs/{urllib.parse.quote(run_id)}/items", item)


@dataclass
class ExperimentResult:
    run_id: str
    run_name: str
    items: int
    errors: int
    # Mean of each numeric or boolean score name, over the items that have it.
    scores: dict[str, float] = field(default_factory=lambda: {})
    evaluator_errors: list[str] = field(default_factory=lambda: [])
    # Items whose result the server did not accept, with the reason; the run goes on.
    failed_posts: list[str] = field(default_factory=lambda: [])


def run_experiment(
    *,
    dataset: str,
    name: str,
    task: Task,
    evaluators: Sequence[Evaluator] = (),
    description: str | None = None,
    metadata: dict[str, Any] | None = None,
    concurrency: int = 4,
    base_url: str = DEFAULT_URL,
) -> ExperimentResult:
    """Run `task` on every active item of `dataset`, score it, and record a run named `name`."""
    client = Client(base_url)
    found = client.dataset(dataset)
    items = client.items(found["id"])
    run = client.create_run(found["id"], name, description=description, metadata=metadata)
    evaluator_errors: list[str] = []
    failed_posts: list[str] = []
    totals: dict[str, list[float]] = {}

    def one(item: dict[str, Any]) -> bool:
        start = time.time_ns()
        output: Any = None
        error: str | None = None
        try:
            output = task(item["input"])
        except Exception as exc:  # the task's failure is a result, not a crash
            error = f"{type(exc).__name__}: {exc}"[:2000]
        end = time.time_ns()
        item_scores: list[dict[str, Any]] = []
        if error is None:
            for evaluator in evaluators:
                try:
                    found = _scores(evaluator, item["input"], output, item.get("expected_output"))
                except Exception as exc:
                    evaluator_errors.append(
                        f"{_name(evaluator)} on {item['id']}: {type(exc).__name__}: {exc}"
                    )
                    continue
                for score in found:
                    problem = _score_problem(score)
                    if problem:
                        evaluator_errors.append(f"{_name(evaluator)} on {item['id']}: {problem}")
                    else:
                        item_scores.append(score)
        result = {
            "item_id": item["id"],
            "output": _jsonable(output),
            "error": error,
            "start_ns": start,
            "end_ns": end,
            "scores": item_scores,
        }
        try:
            client.add_run_item(run["id"], result)
        except AgentwatchError as refused:
            # Recorded and skipped, so one refused result cannot stop the experiment.
            failed_posts.append(f"{item['id']}: {refused}")
            return error is None
        for score in item_scores:
            value = score.get("value")
            if isinstance(value, bool | int | float):
                totals.setdefault(score["name"], []).append(float(value))
        return error is None

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        outcomes = list(pool.map(one, items))
    return ExperimentResult(
        run_id=run["id"],
        run_name=run["name"],
        items=len(items),
        errors=outcomes.count(False),
        scores={name: sum(values) / len(values) for name, values in sorted(totals.items())},
        evaluator_errors=evaluator_errors,
        failed_posts=failed_posts,
    )


def _name(evaluator: Evaluator) -> str:
    return getattr(evaluator, "__name__", type(evaluator).__name__)


def _scores(evaluator: Evaluator, input: Any, output: Any, expected: Any) -> list[dict[str, Any]]:
    """Normalise what an evaluator returned into score dicts."""
    result = evaluator(input, output, expected)
    results: list[Any] = result if isinstance(result, list) else [result]  # pyright: ignore[reportUnknownVariableType]
    found: list[dict[str, Any]] = []
    for value in results:  # pyright: ignore[reportUnknownVariableType]
        if value is None:
            continue
        if isinstance(value, dict):
            found.append({"name": _name(evaluator), **value})  # pyright: ignore[reportUnknownArgumentType]
        elif isinstance(value, bool | int | float):
            found.append({"name": _name(evaluator), "value": value})
        elif isinstance(value, str):
            found.append({"name": _name(evaluator), "label": value})
        else:
            raise TypeError(f"an evaluator returned {type(value).__name__}")  # pyright: ignore[reportUnknownArgumentType]
    return found


def _score_problem(score: dict[str, Any]) -> str | None:
    """Why the server would refuse this score, caught here so one score cannot fail an item."""
    name = score.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        return "score name must be 1 to 200 characters"
    value = score.get("value")
    if isinstance(value, float) and not math.isfinite(value):
        return f"score {name!r} is not a finite number"
    if value is None and not score.get("label"):
        return f"score {name!r} has no value or label"
    return None


def _jsonable(value: Any) -> Any:
    """Outputs are sent as JSON; anything else is sent as its text."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return repr(value)
    return value
