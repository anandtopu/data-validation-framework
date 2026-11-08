"""Compare benchmark results with a baseline and fail on slowdowns of more than 15%.

Usage::

    python tests/benchmarks/compare.py .benchmarks/results.json .benchmarks/baseline.json [--update]

The baseline belongs to one machine type (CI keeps one per runner OS in its cache). Missing
baselines pass, so the first run on a machine records one with ``--update``.
"""

# ruff: noqa: T201 - a command-line script that reports on stdout

from __future__ import annotations

import json
import sys
from pathlib import Path

THRESHOLD = 0.15
TIMED = ("seconds",)


def compare(
    results: dict[str, dict[str, float]], baseline: dict[str, dict[str, float]]
) -> list[str]:
    problems = []
    for name, metrics in results.items():
        before = baseline.get(name)
        if not before or before.get("rows") != metrics.get("rows"):
            continue
        for key in TIMED:
            if key in metrics and key in before and before[key] > 0:
                change = metrics[key] / before[key] - 1
                status = "SLOWER" if change > THRESHOLD else "ok"
                print(
                    f"{name}.{key}: {before[key]:.2f} -> {metrics[key]:.2f} ({change:+.1%}) {status}"
                )
                if change > THRESHOLD:
                    problems.append(f"{name} is {change:.0%} slower than the baseline")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    results_path, baseline_path = Path(argv[0]), Path(argv[1])
    results = json.loads(results_path.read_text(encoding="utf-8"))
    baseline = (
        json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else {}
    )
    problems = compare(results, baseline)
    if "--update" in argv and not problems:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"baseline updated: {baseline_path}")
    for problem in problems:
        print(f"error: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
