"""Fork-only guard for ``scripts/sync_e2e_counts.py``.

The sync script rewrites the five hardcoded E2E case counts with regexes, so it
breaks silently if upstream reshapes one of those files.  These tests re-render
the count files from the numbers the manifest *already* carries: the render must
succeed (every pattern still matches exactly once) and must be a byte-for-byte
no-op.  Deliberately independent of a live pytest collection, so a tree whose
counts are momentarily stale does not turn this lane red.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sync_e2e_counts as sync  # noqa: E402


def _manifest():
    return json.loads(sync.SHARDS_JSON.read_text(encoding="utf-8"))


def test_rendering_the_manifest_counts_is_a_byte_for_byte_no_op():
    manifest = _manifest()
    per_shard = [(shard["id"], shard["collected_cases"]) for shard in manifest["shards"]]

    rendered = sync.render(manifest["expected_total"], per_shard)

    assert set(rendered) == {
        sync.SHARDS_JSON,
        sync.SHARDS_SCRIPT,
        sync.SHARDS_TEST,
        sync.CONTRACTS_TEST,
        sync.FULL_E2E,
    }
    for path, text in rendered.items():
        with open(path, encoding="utf-8", newline="") as handle:
            assert text == handle.read(), f"{path.name} would be rewritten"


def test_rendering_a_different_total_touches_every_count_file():
    manifest = _manifest()
    bumped = manifest["expected_total"] + 1
    per_shard = [
        (shard["id"], shard["collected_cases"] + (1 if index == 0 else 0))
        for index, shard in enumerate(manifest["shards"])
    ]

    rendered = sync.render(bumped, per_shard)

    for path, text in rendered.items():
        with open(path, encoding="utf-8", newline="") as handle:
            assert text != handle.read(), f"{path.name} kept a stale count"
        assert str(bumped) in text


def test_render_list_keeps_indentation_and_line_endings():
    body = "\r\n    1,\r\n    2,"

    assert sync._render_list(body, [7, 8]) == "\r\n    7,\r\n    8,"
