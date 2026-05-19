"""Enforce per-package coverage gates from coverage.json.

The global ``fail_under`` in ``[tool.coverage.report]`` enforces the
total threshold. Per-package floors (defined in
``[tool.heddle.coverage-gates]`` of ``pyproject.toml``) are not
natively supported by coverage.py, so this script reads the JSON
report produced by ``pytest --cov-report=json`` and checks each
package's branch-aware coverage against its configured floor.

Usage::

    uv run pytest --cov --cov-report=json
    uv run python tools/check_coverage_gates.py

The script aggregates per file under ``src/heddle/<package>/...``,
treating ``src/heddle/contrib/<name>/...`` as ``contrib/<name>``.
Coverage is computed branch-aware: ``(stmts - miss + cov_branches) /
(stmts + branches)``.

Exits 0 on pass, 1 on any package below floor (with a per-package
diff), 2 on configuration/IO errors. See
``docs/CODING_GUIDE.md`` "Coverage gates and the ratchet rule".
"""

from __future__ import annotations

import json
import math
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COVERAGE_JSON = REPO_ROOT / "coverage.json"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _package_for(path: str) -> str | None:
    """Map a coverage source path to its package key."""
    parts = Path(path).parts
    if "heddle" not in parts:
        return None
    rest = parts[parts.index("heddle") + 1 :]
    if not rest:
        return None
    head = rest[0]
    if head == "contrib" and len(rest) > 2:
        return f"contrib/{rest[1]}"
    if head.endswith(".py"):
        return None
    return head


def _aggregate(coverage_data: dict) -> dict[str, dict[str, int]]:
    agg: dict[str, dict[str, int]] = defaultdict(
        lambda: {"stmts": 0, "miss": 0, "branches": 0, "cov_branches": 0}
    )
    for path, info in coverage_data.get("files", {}).items():
        pkg = _package_for(path)
        if pkg is None:
            continue
        s = info["summary"]
        a = agg[pkg]
        a["stmts"] += s["num_statements"]
        a["miss"] += s["missing_lines"]
        a["branches"] += s.get("num_branches", 0)
        a["cov_branches"] += s.get("covered_branches", 0)
    return agg


def _coverage_pct(a: dict[str, int]) -> float:
    denom = a["stmts"] + a["branches"]
    if denom == 0:
        return 100.0
    num = a["stmts"] - a["miss"] + a["cov_branches"]
    return 100.0 * num / denom


def main() -> int:
    if not COVERAGE_JSON.exists():
        print(
            f"check_coverage_gates: {COVERAGE_JSON.relative_to(REPO_ROOT)} not "
            "found. Run `pytest --cov --cov-report=json` first.",
            file=sys.stderr,
        )
        return 2

    with PYPROJECT.open("rb") as f:
        pyproject = tomllib.load(f)
    gates = pyproject.get("tool", {}).get("heddle", {}).get("coverage-gates", {})
    if not gates:
        print(
            "check_coverage_gates: no [tool.heddle.coverage-gates] table in pyproject.toml.",
            file=sys.stderr,
        )
        return 2

    with COVERAGE_JSON.open() as f:
        coverage_data = json.load(f)
    agg = _aggregate(coverage_data)

    failures: list[tuple[str, float, float]] = []
    rows: list[tuple[str, float, float, str]] = []
    for pkg, floor in sorted(gates.items()):
        a = agg.get(pkg)
        if a is None or a["stmts"] == 0:
            rows.append((pkg, math.nan, float(floor), "MISSING"))
            failures.append((pkg, math.nan, float(floor)))
            continue
        cov = _coverage_pct(a)
        status = "OK" if cov >= floor else "FAIL"
        if cov < floor:
            failures.append((pkg, cov, float(floor)))
        rows.append((pkg, cov, float(floor), status))

    width = max(len(r[0]) for r in rows)
    print(f"{'Package':<{width}}  {'Cov%':>7}  {'Floor':>6}  Status")
    print("-" * (width + 28))
    for pkg, cov, floor, status in rows:
        cov_str = f"{cov:7.2f}" if not math.isnan(cov) else "    n/a"
        print(f"{pkg:<{width}}  {cov_str}  {floor:6.0f}  {status}")

    if failures:
        print()
        print("FAIL: the following packages are below their coverage floor:")
        for pkg, cov, floor in failures:
            cov_str = f"{cov:.2f}%" if not math.isnan(cov) else "no data"
            print(f"  - {pkg}: {cov_str} < {floor:.0f}%")
        print()
        print(
            "See docs/CODING_GUIDE.md 'Coverage gates and the ratchet rule' "
            "for the policy on raising/lowering floors."
        )
        return 1

    print()
    print(f"OK: all {len(rows)} per-package gates passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
