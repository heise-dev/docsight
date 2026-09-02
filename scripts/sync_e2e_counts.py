#!/usr/bin/env python3
"""Rewrite the hardcoded E2E case counts from a live pytest collection.

Fork-only helper (never part of an upstream contribution): upstream keeps the
counts in five files by hand, which makes every task branch that adds an E2E
case conflict with every other one.  On ``deploy`` the numbers are recomputed
by ``.github/workflows/e2e-count-sync.yml`` after each merge instead.

The five places a count lives:

1. ``tests/e2e/shards.json``            -- ``expected_total`` + per-shard ``collected_cases``
2. ``scripts/e2e_shards.py``           -- ``EXPECTED_TOTAL``
3. ``tests/test_e2e_shards.py``        -- test function *name*, the
   ``== EXPECTED_TOTAL == N`` assert, the per-shard list, ``--expected-total N``
4. ``tests/test_ci_workflow_contracts.py`` -- ``--expected-total N``
5. ``.github/workflows/full-e2e.yml``  -- the step *name* and the
   ``--expected-total N`` flag

Usage::

    python scripts/sync_e2e_counts.py            # rewrite in place
    python scripts/sync_e2e_counts.py --check    # exit 1 if a file would change

Line endings are preserved byte for byte (``newline=""`` on both ends) and the
rewrite is idempotent: a tree that already carries the live counts is untouched.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_shards import (  # noqa: E402  (path shim above must run first)
    E2E_DIR,
    ROOT,
    load_manifest,
)

SHARDS_JSON = E2E_DIR / "shards.json"
SHARDS_SCRIPT = ROOT / "scripts" / "e2e_shards.py"
SHARDS_TEST = ROOT / "tests" / "test_e2e_shards.py"
CONTRACTS_TEST = ROOT / "tests" / "test_ci_workflow_contracts.py"
FULL_E2E = ROOT / ".github" / "workflows" / "full-e2e.yml"


class SyncError(RuntimeError):
    """The tree no longer matches the shapes this script knows how to rewrite."""


def collect_cases_per_file() -> dict[str, int]:
    """Collect ``tests/e2e`` once and count node ids per file.

    Same collection command as ``scripts/e2e_shards.py collect`` (which runs it
    per shard); counting per file lets one run answer both the total and every
    shard.  Requires the ``playwright`` Python package -- the test modules do
    ``from playwright.sync_api import expect`` at import time -- but no browser
    binaries, since collection never launches one.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(E2E_DIR)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        sys.stdout.write(completed.stdout)
        raise SyncError(f"pytest collection failed with {completed.returncode}")
    per_file: dict[str, int] = {}
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line.startswith("tests/e2e/") or "::" not in line:
            continue
        name = line.split("::", 1)[0].removeprefix("tests/e2e/")
        per_file[name] = per_file.get(name, 0) + 1
    if not per_file:
        sys.stdout.write(completed.stdout)
        raise SyncError("pytest collected no E2E node ids")
    return per_file


def shard_counts(manifest: dict, per_file: dict[str, int]) -> list[tuple[int, int]]:
    counts = []
    seen: set[str] = set()
    for shard in manifest["shards"]:
        total = 0
        for name in shard["files"]:
            if name not in per_file:
                raise SyncError(f"manifest file {name} collected no cases")
            total += per_file[name]
            seen.add(name)
        counts.append((shard["id"], total))
    missing = set(per_file) - seen
    if missing:
        raise SyncError(f"collected files missing from the manifest: {sorted(missing)}")
    return counts


def _sub(text: str, pattern: str, repl: str, path: Path, *, expected: int = 1) -> str:
    new, count = re.subn(pattern, repl, text)
    if count != expected:
        raise SyncError(
            f"{path.relative_to(ROOT)}: pattern {pattern!r} matched {count}x, "
            f"expected {expected}x -- the file's shape changed"
        )
    return new


def _read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def render(total: int, per_shard: list[tuple[int, int]]) -> dict[Path, str]:
    """Return the desired content of every count file."""
    flag = rf"(--expected-total )\d+"

    shards_json = _read(SHARDS_JSON)
    shards_json = _sub(
        shards_json,
        r'(?m)^(\s*"expected_total":\s*)\d+',
        lambda m: f"{m.group(1)}{total}",
        SHARDS_JSON,
    )
    for shard_id, count in per_shard:
        shards_json = _sub(
            shards_json,
            rf'(?s)("id":\s*{shard_id},\s*"collected_cases":\s*)\d+',
            lambda m, c=count: f"{m.group(1)}{c}",
            SHARDS_JSON,
        )

    script = _sub(
        _read(SHARDS_SCRIPT),
        r"(?m)^(EXPECTED_TOTAL = )\d+$",
        lambda m: f"{m.group(1)}{total}",
        SHARDS_SCRIPT,
    )

    shards_test = _read(SHARDS_TEST)
    shards_test = _sub(
        shards_test,
        r"(?m)^(def test_repository_manifest_covers_every_e2e_file_once_and_)\d+(_cases)",
        lambda m: f"{m.group(1)}{total}{m.group(2)}",
        SHARDS_TEST,
    )
    shards_test = _sub(
        shards_test,
        r"(== EXPECTED_TOTAL == )\d+",
        lambda m: f"{m.group(1)}{total}",
        SHARDS_TEST,
    )
    shards_test = _sub(
        shards_test,
        r'(?s)(\[shard\["collected_cases"\] for shard in manifest\["shards"\]\] == \[)'
        r"(.*?)(\n\s*\])",
        lambda m: m.group(1)
        + _render_list(m.group(2), [count for _, count in per_shard])
        + m.group(3),
        SHARDS_TEST,
    )
    shards_test = _sub(
        shards_test, flag, lambda m: f"{m.group(1)}{total}", SHARDS_TEST
    )

    contracts = _sub(
        _read(CONTRACTS_TEST), flag, lambda m: f"{m.group(1)}{total}", CONTRACTS_TEST
    )

    workflow = _read(FULL_E2E)
    workflow = _sub(
        workflow,
        r"(Verify complete successful )\d+(-case union)",
        lambda m: f"{m.group(1)}{total}{m.group(2)}",
        FULL_E2E,
    )
    workflow = _sub(workflow, flag, lambda m: f"{m.group(1)}{total}", FULL_E2E)

    return {
        SHARDS_JSON: shards_json,
        SHARDS_SCRIPT: script,
        SHARDS_TEST: shards_test,
        CONTRACTS_TEST: contracts,
        FULL_E2E: workflow,
    }


def _render_list(body: str, counts: list[int]) -> str:
    """Rewrite the per-shard literal list, keeping its newline and indentation."""
    entries = re.findall(r"(\r?\n)(\s*)\d+,", body)
    if len(entries) != len(counts):
        raise SyncError(
            f"per-shard list has {len(entries)} entries, expected {len(counts)}"
        )
    return "".join(
        f"{newline}{indent}{count},"
        for (newline, indent), count in zip(entries, counts, strict=True)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if any file would change",
    )
    args = parser.parse_args(argv)

    # Only the manifest's file->shard mapping is trusted here: the counts in it
    # are the very thing being recomputed, so `validate_manifest` (which asserts
    # they already add up) must not gate the sync -- after a merge that took one
    # side of a count conflict they deliberately do not.
    manifest = load_manifest(SHARDS_JSON)
    per_file = collect_cases_per_file()
    total = sum(per_file.values())
    per_shard = shard_counts(manifest, per_file)
    if sum(count for _, count in per_shard) != total:
        raise SyncError("shard counts do not sum to the collected total")

    rendered = render(total, per_shard)
    changed = [path for path, text in rendered.items() if _read(path) != text]
    for path in changed:
        if not args.check:
            _write(path, rendered[path])

    summary = ", ".join(f"shard {i} {c}" for i, c in per_shard)
    print(f"collected {total} cases ({summary})")
    if not changed:
        print("counts already in sync")
        return 0
    verb = "would rewrite" if args.check else "rewrote"
    for path in changed:
        print(f"{verb} {path.relative_to(ROOT)}")
    return 1 if args.check else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
