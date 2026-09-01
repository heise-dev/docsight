"""Tests for Connection Monitor storage layer."""

import math
import random
import sqlite3
import time
from datetime import datetime
import pytest

from app.modules.connection_monitor.storage import ConnectionMonitorStorage


@pytest.fixture
def storage(tmp_path):
    db_path = str(tmp_path / "test_cm.db")
    return ConnectionMonitorStorage(db_path)


def _save_buckets(storage, target_id, bucket_seconds, buckets):
    """Insert aggregated buckets directly, bypassing the aggregation cascade."""
    with storage._connect() as conn:
        for b in buckets:
            conn.execute(
                """INSERT INTO connection_samples_aggregated
                   (target_id, bucket_start, bucket_seconds, avg_latency_ms,
                    min_latency_ms, max_latency_ms, p95_latency_ms,
                    packet_loss_pct, sample_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (target_id, b["bucket_start"], bucket_seconds,
                 b.get("avg_latency_ms"), b.get("min_latency_ms"),
                 b.get("max_latency_ms"), b.get("p95_latency_ms"),
                 b["packet_loss_pct"], b["sample_count"]),
            )


def _local_midday(timestamp):
    """Midday of the local day a timestamp falls in, away from date edges."""
    return datetime.fromtimestamp(timestamp).replace(
        hour=12, minute=0, second=0, microsecond=0
    ).timestamp()


class TestTargetCRUD:
    def test_create_target(self, storage):
        tid = storage.create_target("Cloudflare", "1.1.1.1")
        assert tid == 1
        targets = storage.get_targets()
        assert len(targets) == 1
        assert targets[0]["label"] == "Cloudflare"
        assert targets[0]["host"] == "1.1.1.1"
        assert targets[0]["enabled"] == 1
        assert targets[0]["poll_interval_ms"] == 5000

    def test_create_target_custom_settings(self, storage):
        tid = storage.create_target(
            "Google", "8.8.8.8",
            poll_interval_ms=2500, probe_method="tcp", tcp_port=80,
        )
        target = storage.get_target(tid)
        assert target["poll_interval_ms"] == 2500
        assert target["probe_method"] == "tcp"
        assert target["tcp_port"] == 80

    def test_update_target(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        storage.update_target(tid, label="Updated", enabled=False)
        target = storage.get_target(tid)
        assert target["label"] == "Updated"
        assert target["enabled"] == 0

    def test_delete_target_cascades_samples(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        storage.save_samples([
            {"target_id": tid, "timestamp": time.time(), "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])
        storage.delete_target(tid)
        assert storage.get_target(tid) is None
        assert storage.get_samples(tid) == []

    def test_purge_demo_targets_preserves_user_targets_and_samples(self, storage):
        user_id = storage.create_target("User", "192.0.2.1")
        demo_id = storage.create_target("Demo", "198.51.100.1", is_demo=True)
        storage.save_samples([
            {"target_id": user_id, "timestamp": time.time(), "latency_ms": 8.0,
             "timeout": False, "probe_method": "tcp"},
            {"target_id": demo_id, "timestamp": time.time(), "latency_ms": 18.0,
             "timeout": False, "probe_method": "tcp"},
        ])

        assert storage.purge_demo_targets() == 1

        assert storage.get_target(user_id)["is_demo"] == 0
        assert len(storage.get_samples(user_id)) == 1
        assert storage.get_target(demo_id) is None
        assert storage.get_samples(demo_id) == []

    def test_existing_target_table_gets_non_demo_provenance_column(self, tmp_path):
        db_path = tmp_path / "legacy_cm.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE connection_targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    label TEXT NOT NULL,
                    host TEXT NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    poll_interval_ms INTEGER NOT NULL DEFAULT 5000,
                    probe_method TEXT NOT NULL DEFAULT 'auto',
                    tcp_port INTEGER NOT NULL DEFAULT 443,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute(
                "INSERT INTO connection_targets (label, host, created_at) "
                "VALUES ('Existing', '203.0.113.1', ?)",
                (time.time(),),
            )

        migrated = ConnectionMonitorStorage(str(db_path))

        assert migrated.get_targets()[0]["is_demo"] == 0
        assert migrated.purge_demo_targets() == 0
        assert len(migrated.get_targets()) == 1

    def test_get_nonexistent_target(self, storage):
        assert storage.get_target(999) is None


class TestSamples:
    def test_save_and_get_samples(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        samples = [
            {"target_id": tid, "timestamp": now - 2, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now - 1, "latency_ms": 15.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now, "latency_ms": None, "timeout": True, "probe_method": "tcp"},
        ]
        storage.save_samples(samples)
        result = storage.get_samples(tid)
        assert len(result) == 3

    def test_get_samples_with_time_range(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        samples = [
            {"target_id": tid, "timestamp": now - 100, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now - 50, "latency_ms": 15.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now, "latency_ms": 20.0, "timeout": False, "probe_method": "tcp"},
        ]
        storage.save_samples(samples)
        result = storage.get_samples(tid, start=now - 60, end=now - 10)
        assert len(result) == 1
        assert result[0]["latency_ms"] == 15.0

    def test_get_samples_with_limit(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        samples = [
            {"target_id": tid, "timestamp": now - i, "latency_ms": float(i), "timeout": False, "probe_method": "tcp"}
            for i in range(20)
        ]
        storage.save_samples(samples)
        result = storage.get_samples(tid, limit=5)
        assert len(result) == 5

    def test_get_samples_no_limit(self, storage):
        """limit=0 should return all samples (no LIMIT clause)."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        samples = [
            {"target_id": tid, "timestamp": now - i, "latency_ms": float(i), "timeout": False, "probe_method": "tcp"}
            for i in range(20)
        ]
        storage.save_samples(samples)
        result = storage.get_samples(tid, limit=0)
        assert len(result) == 20

    def test_get_samples_limit_above_row_count_matches_unlimited(self, storage):
        """A limit past the last row is the same read as no limit at all."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        storage.save_samples([
            {"target_id": tid, "timestamp": now - i, "latency_ms": float(i),
             "timeout": False, "probe_method": "tcp"}
            for i in range(20)
        ])
        assert storage.get_samples(tid, limit=1000) == storage.get_samples(tid, limit=0)


def _reference_buckets(rows, anchor, width):
    """Bucket rows the way get_samples_bucketed is specified to."""
    groups = {}
    for row in rows:
        groups.setdefault(int((row["timestamp"] - anchor) // width), []).append(row)
    buckets = []
    for idx in sorted(groups):
        members = groups[idx]
        latencies = sorted(m["latency_ms"] for m in members if m["latency_ms"] is not None)
        timeouts = sum(1 for m in members if m["timeout"])
        buckets.append({
            "bucket_start": anchor + idx * width,
            "bucket_seconds": width,
            "avg_latency_ms": (sum(latencies) / len(latencies)) if latencies else None,
            "min_latency_ms": latencies[0] if latencies else None,
            "max_latency_ms": latencies[-1] if latencies else None,
            "p95_latency_ms": latencies[math.floor(len(latencies) * 0.95)] if latencies else None,
            "packet_loss_pct": round(100.0 * timeouts / len(members), 2),
            "sample_count": len(members),
        })
    return buckets


class TestBucketedSamples:
    def test_matches_a_python_reference(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        rng = random.Random(99)
        anchor = 1_700_000_000.5
        width = 37
        rows = []
        ts = anchor
        while ts < anchor + 20 * width:
            timeout = rng.random() < 0.1
            rows.append({
                "target_id": tid, "timestamp": ts, "probe_method": "tcp",
                "timeout": timeout,
                # duplicates on purpose: nearest-rank p95 must pick by value
                "latency_ms": None if timeout else rng.choice([8.5, 8.5, 12.25, 12.25, 60.0]),
            })
            ts += rng.uniform(0.4, 2.5)
        storage.save_samples(rows)

        buckets = storage.get_samples_bucketed(
            tid, start=anchor, end=ts, bucket_seconds=width, anchor=anchor,
        )
        expected = _reference_buckets(rows, anchor, width)
        assert len(buckets) == len(expected)
        for got, want in zip(buckets, expected):
            for key, value in want.items():
                if key == "avg_latency_ms" and value is not None:
                    assert got[key] == pytest.approx(value)
                else:
                    assert got[key] == value

    def test_timeout_rows_only_add_to_count_and_loss(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        anchor = 1_700_000_000.0
        storage.save_samples([
            {"target_id": tid, "timestamp": anchor, "latency_ms": 10.0,
             "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": anchor + 1, "latency_ms": None,
             "timeout": True, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": anchor + 2, "latency_ms": 20.0,
             "timeout": False, "probe_method": "tcp"},
            # A timeout that still measured a latency counts towards the latency
            # stats, exactly like the row-by-row compressor treats it.
            {"target_id": tid, "timestamp": anchor + 3, "latency_ms": 90.0,
             "timeout": True, "probe_method": "tcp"},
        ])
        bucket = storage.get_samples_bucketed(
            tid, start=anchor, end=anchor + 10, bucket_seconds=10, anchor=anchor,
        )[0]
        assert bucket["sample_count"] == 4
        assert bucket["packet_loss_pct"] == 50.0
        assert bucket["min_latency_ms"] == 10.0
        assert bucket["max_latency_ms"] == 90.0
        assert bucket["avg_latency_ms"] == pytest.approx(40.0)

    def test_all_timeout_bucket_has_no_latency_stats(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        anchor = 1_700_000_000.0
        storage.save_samples([
            {"target_id": tid, "timestamp": anchor + i, "latency_ms": None,
             "timeout": True, "probe_method": "tcp"}
            for i in range(5)
        ])
        bucket = storage.get_samples_bucketed(
            tid, start=anchor, end=anchor + 10, bucket_seconds=10, anchor=anchor,
        )[0]
        assert bucket["sample_count"] == 5
        assert bucket["packet_loss_pct"] == 100.0
        assert bucket["avg_latency_ms"] is None
        assert bucket["min_latency_ms"] is None
        assert bucket["max_latency_ms"] is None
        assert bucket["p95_latency_ms"] is None

    def test_buckets_are_indexed_from_the_anchor(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        anchor = 1_700_000_000.25
        storage.save_samples([
            {"target_id": tid, "timestamp": anchor + 130 + i, "latency_ms": 5.0,
             "timeout": False, "probe_method": "tcp"}
            for i in range(3)
        ])
        buckets = storage.get_samples_bucketed(
            tid, start=anchor + 60, end=anchor + 240, bucket_seconds=60, anchor=anchor,
        )
        assert [b["bucket_start"] for b in buckets] == [anchor + 120]
        assert buckets[0]["sample_count"] == 3

    def test_empty_range_returns_no_buckets(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        assert storage.get_samples_bucketed(
            tid, start=0.0, end=100.0, bucket_seconds=10, anchor=0.0,
        ) == []


class TestPinnedDays:
    def test_pin_day(self, storage):
        storage.pin_day("2026-03-10")
        days = storage.get_pinned_days()
        assert len(days) == 1
        assert days[0]["date"] == "2026-03-10"
        assert days[0]["label"] is None

    def test_pin_day_with_label(self, storage):
        storage.pin_day("2026-03-10", label="Outage investigation")
        days = storage.get_pinned_days()
        assert days[0]["label"] == "Outage investigation"

    def test_pin_day_idempotent(self, storage):
        storage.pin_day("2026-03-10")
        storage.pin_day("2026-03-10")
        days = storage.get_pinned_days()
        assert len(days) == 1

    def test_unpin_day(self, storage):
        storage.pin_day("2026-03-10")
        assert storage.unpin_day("2026-03-10") is True
        assert storage.get_pinned_days() == []

    def test_unpin_nonexistent(self, storage):
        assert storage.unpin_day("2026-01-01") is False

    def test_is_day_pinned(self, storage):
        assert storage.is_day_pinned("2026-03-10") is False
        storage.pin_day("2026-03-10")
        assert storage.is_day_pinned("2026-03-10") is True

    def test_cleanup_skips_pinned_days(self, storage):
        """Pinned day samples should survive cleanup."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        # Sample from 10 days ago
        from datetime import datetime
        old_ts = now - (10 * 86400)
        old_date = datetime.fromtimestamp(old_ts).strftime("%Y-%m-%d")
        storage.pin_day(old_date)
        storage.save_samples([
            {"target_id": tid, "timestamp": old_ts, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])
        deleted = storage.cleanup(retention_days=7)
        assert deleted == 0
        assert len(storage.get_samples(tid)) == 1

    def test_cleanup_deletes_unpinned(self, storage):
        """After unpinning, cleanup should delete old samples."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        from datetime import datetime
        old_ts = now - (10 * 86400)
        old_date = datetime.fromtimestamp(old_ts).strftime("%Y-%m-%d")
        storage.pin_day(old_date)
        storage.save_samples([
            {"target_id": tid, "timestamp": old_ts, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])
        # Unpin and cleanup
        storage.unpin_day(old_date)
        deleted = storage.cleanup(retention_days=7)
        assert deleted == 1
        assert len(storage.get_samples(tid)) == 0

    def test_aggregation_skips_pinned_days(self, storage):
        """Raw samples for pinned days should survive aggregation."""
        tid = storage.create_target("Test", "1.1.1.1")
        from datetime import datetime
        base = 1700000000.0
        base_date = datetime.fromtimestamp(base + 5).strftime("%Y-%m-%d")
        storage.pin_day(base_date)
        storage.save_samples([
            {"target_id": tid, "timestamp": base + 5, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": base + 10, "latency_ms": 20.0, "timeout": False, "probe_method": "tcp"},
        ])
        storage.aggregate_raw_to_buckets(tid, cutoff=base + 100, bucket_seconds=60)
        # Raw samples should still exist (pinned)
        raw = storage.get_samples(tid)
        assert len(raw) == 2
        # Aggregated bucket should still be created
        agg = storage.get_aggregated_samples(tid, bucket_seconds=60)
        assert len(agg) == 1


class TestRetention:
    def test_cleanup_old_samples(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        old_ts = now - (8 * 86400)  # 8 days ago
        new_ts = now - 60  # 1 minute ago
        storage.save_samples([
            {"target_id": tid, "timestamp": old_ts, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": new_ts, "latency_ms": 20.0, "timeout": False, "probe_method": "tcp"},
        ])
        deleted = storage.cleanup(retention_days=7)
        assert deleted == 1
        result = storage.get_samples(tid)
        assert len(result) == 1
        assert result[0]["latency_ms"] == 20.0

    def test_cleanup_zero_keeps_all(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        old_ts = time.time() - (365 * 86400)
        storage.save_samples([
            {"target_id": tid, "timestamp": old_ts, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])
        deleted = storage.cleanup(retention_days=0)
        assert deleted == 0
        assert len(storage.get_samples(tid)) == 1

    def test_cleanup_deletes_old_aggregated_data(self, storage):
        """cleanup() should also delete old aggregated samples."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        old_ts = now - 200 * 86400  # 200 days ago

        # Insert an aggregated bucket
        with storage._connect() as conn:
            conn.execute(
                """INSERT INTO connection_samples_aggregated
                   (target_id, bucket_start, bucket_seconds,
                    avg_latency_ms, min_latency_ms, max_latency_ms,
                    p95_latency_ms, packet_loss_pct, sample_count)
                   VALUES (?, ?, 3600, 10.0, 5.0, 20.0, 18.0, 0.0, 100)""",
                (tid, old_ts),
            )

        deleted = storage.cleanup(retention_days=180)
        assert deleted == 1  # exactly one aggregated row

        agg = storage.get_aggregated_samples(tid, bucket_seconds=3600)
        assert len(agg) == 0


class TestSummary:
    def test_summary_returns_stats(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        storage.save_samples([
            {"target_id": tid, "timestamp": now - 30, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now - 20, "latency_ms": 20.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now - 10, "latency_ms": None, "timeout": True, "probe_method": "tcp"},
        ])
        summary = storage.get_summary(tid, window_seconds=60)
        assert summary["sample_count"] == 3
        assert summary["avg_latency_ms"] == 15.0
        assert abs(summary["packet_loss_pct"] - 33.33) < 1
        assert summary["min_latency_ms"] == 10.0
        assert summary["max_latency_ms"] == 20.0

    def test_range_stats_returns_exact_metrics(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        storage.save_samples([
            {"target_id": tid, "timestamp": now - 40, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now - 30, "latency_ms": 20.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now - 20, "latency_ms": 30.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now - 10, "latency_ms": None, "timeout": True, "probe_method": "tcp"},
        ])
        stats = storage.get_range_stats(tid, start=now - 60, end=now)
        assert stats["sample_count"] == 4
        assert stats["latency_count"] == 3
        assert stats["avg_latency_ms"] == 20.0
        assert stats["min_latency_ms"] == 10.0
        assert stats["max_latency_ms"] == 30.0
        assert stats["p95_latency_ms"] == 30.0
        assert abs(stats["packet_loss_pct"] - 25.0) < 0.01
        assert stats["tiers_used"] == ["raw"]

    def test_range_stats_empty_window(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        stats = storage.get_range_stats(tid, start=now - 60, end=now)
        assert stats["sample_count"] == 0
        assert stats["avg_latency_ms"] is None
        assert stats["p95_latency_ms"] is None
        assert stats["tiers_used"] == []


class TestTieredRangeStats:
    """Windows reaching past the raw retention must read the bucket tiers."""

    def test_range_stats_from_60s_buckets_only(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        base = ((now - 10 * 86400) // 60) * 60
        _save_buckets(storage, tid, 60, [
            {"bucket_start": base, "avg_latency_ms": 10.0, "min_latency_ms": 5.0,
             "max_latency_ms": 20.0, "p95_latency_ms": 18.0,
             "packet_loss_pct": 0.0, "sample_count": 60},
            {"bucket_start": base + 60, "avg_latency_ms": 30.0, "min_latency_ms": 25.0,
             "max_latency_ms": 40.0, "p95_latency_ms": 38.0,
             "packet_loss_pct": 50.0, "sample_count": 60},
            {"bucket_start": base + 120, "avg_latency_ms": None, "min_latency_ms": None,
             "max_latency_ms": None, "p95_latency_ms": None,
             "packet_loss_pct": 100.0, "sample_count": 40},
        ])
        stats = storage.get_range_stats(tid, start=base - 60, end=base + 180)
        # latency estimates: 60 + 30 + 0 = 90 of 160 samples
        assert stats["sample_count"] == 160
        assert stats["latency_count"] == 90
        assert abs(stats["avg_latency_ms"] - 1500.0 / 90.0) < 0.001
        assert stats["min_latency_ms"] == 5.0
        assert stats["max_latency_ms"] == 40.0
        # timeouts: 0 + 30 + 40 = 70 of 160
        assert stats["packet_loss_pct"] == 43.75
        # weighted nearest rank: ceil(0.95 * 90) = 86 -> falls into the 38.0 bucket
        assert stats["p95_latency_ms"] == 38.0
        assert stats["tiers_used"] == ["1min"]

    def test_range_stats_blends_raw_and_60s_buckets(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        base = ((now - 10 * 86400) // 60) * 60
        storage.save_samples([
            {"target_id": tid, "timestamp": now - 30, "latency_ms": 100.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now - 20, "latency_ms": 200.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now - 10, "latency_ms": None, "timeout": True, "probe_method": "tcp"},
        ])
        _save_buckets(storage, tid, 60, [
            {"bucket_start": base, "avg_latency_ms": 50.0, "min_latency_ms": 50.0,
             "max_latency_ms": 50.0, "p95_latency_ms": 50.0,
             "packet_loss_pct": 0.0, "sample_count": 10},
        ])
        stats = storage.get_range_stats(tid, start=base, end=now)
        assert stats["sample_count"] == 13
        assert stats["latency_count"] == 12
        assert abs(stats["avg_latency_ms"] - 800.0 / 12.0) < 0.001
        assert stats["min_latency_ms"] == 50.0
        assert stats["max_latency_ms"] == 200.0
        assert stats["packet_loss_pct"] == 7.69
        # weighted nearest rank: ceil(0.95 * 12) = 12 -> the slowest raw sample
        assert stats["p95_latency_ms"] == 200.0
        assert stats["tiers_used"] == ["raw", "1min"]

    def test_range_stats_tier_boundary_is_exclusive(self, storage):
        """A bucket sitting on the raw boundary must not be counted twice."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        boundary = now - storage._TIER_RAW_MAX_AGE
        storage.save_samples([
            {"target_id": tid, "timestamp": now - 10, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])
        _save_buckets(storage, tid, 60, [
            {"bucket_start": boundary + 60, "avg_latency_ms": 20.0, "min_latency_ms": 20.0,
             "max_latency_ms": 20.0, "p95_latency_ms": 20.0,
             "packet_loss_pct": 0.0, "sample_count": 5},
            {"bucket_start": boundary - 60, "avg_latency_ms": 30.0, "min_latency_ms": 30.0,
             "max_latency_ms": 30.0, "p95_latency_ms": 30.0,
             "packet_loss_pct": 0.0, "sample_count": 7},
        ])
        stats = storage.get_range_stats(tid, start=boundary - 120, end=now)
        # 1 raw sample + the bucket below the boundary; the bucket above it
        # sits in the raw-covered slice and is not added on top of it.
        assert stats["sample_count"] == 8
        assert stats["latency_count"] == 8
        assert stats["tiers_used"] == ["raw", "1min"]

    def test_range_stats_p95_is_weighted_by_bucket_size(self, storage):
        """p95 must weight each bucket p95 by its sample count, not count buckets."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        base = ((now - 10 * 86400) // 60) * 60
        _save_buckets(storage, tid, 60, [
            {"bucket_start": base, "avg_latency_ms": 10.0, "min_latency_ms": 10.0,
             "max_latency_ms": 10.0, "p95_latency_ms": 10.0,
             "packet_loss_pct": 0.0, "sample_count": 1000},
            {"bucket_start": base + 60, "avg_latency_ms": 500.0, "min_latency_ms": 500.0,
             "max_latency_ms": 500.0, "p95_latency_ms": 500.0,
             "packet_loss_pct": 0.0, "sample_count": 10},
        ])
        stats = storage.get_range_stats(tid, start=base, end=base + 120)
        # ceil(0.95 * 1010) = 960 <= 1000, so the wide bucket carries the p95;
        # an unweighted p95 (or max) of the two bucket p95s would say 500.0.
        assert stats["p95_latency_ms"] == 10.0

    def test_range_stats_from_5min_and_1hr_buckets(self, storage):
        """An unbounded window must reach the coarser tiers too."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        _save_buckets(storage, tid, 300, [
            {"bucket_start": ((now - 40 * 86400) // 300) * 300,
             "avg_latency_ms": 25.0, "min_latency_ms": 20.0, "max_latency_ms": 30.0,
             "p95_latency_ms": 28.0, "packet_loss_pct": 0.0, "sample_count": 300},
        ])
        _save_buckets(storage, tid, 3600, [
            {"bucket_start": ((now - 100 * 86400) // 3600) * 3600,
             "avg_latency_ms": 40.0, "min_latency_ms": 35.0, "max_latency_ms": 60.0,
             "p95_latency_ms": 55.0, "packet_loss_pct": 10.0, "sample_count": 3600},
        ])
        stats = storage.get_range_stats(tid)
        assert stats["sample_count"] == 3900
        assert stats["latency_count"] == 3540
        assert abs(stats["avg_latency_ms"] - 137100.0 / 3540.0) < 0.001
        assert stats["min_latency_ms"] == 20.0
        assert stats["max_latency_ms"] == 60.0
        assert stats["packet_loss_pct"] == 9.23
        assert stats["p95_latency_ms"] == 55.0
        assert stats["tiers_used"] == ["5min", "1hr"]

    def test_range_stats_open_ended_window(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        base = ((now - 10 * 86400) // 60) * 60
        storage.save_samples([
            {"target_id": tid, "timestamp": now - 10, "latency_ms": 40.0, "timeout": False, "probe_method": "tcp"},
        ])
        _save_buckets(storage, tid, 60, [
            {"bucket_start": base, "avg_latency_ms": 20.0, "min_latency_ms": 20.0,
             "max_latency_ms": 20.0, "p95_latency_ms": 20.0,
             "packet_loss_pct": 0.0, "sample_count": 9},
        ])
        stats = storage.get_range_stats(tid, start=base - 60, end=None)
        assert stats["sample_count"] == 10
        assert stats["latency_count"] == 10
        assert stats["avg_latency_ms"] == 22.0
        assert stats["tiers_used"] == ["raw", "1min"]

    def test_range_stats_reads_raw_older_than_the_raw_tier(self, storage):
        """Raw that aggregate() has not folded away yet must still be read."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        base = now - 10 * 86400
        storage.save_samples([
            {"target_id": tid, "timestamp": base, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": base + 5, "latency_ms": 20.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": base + 10, "latency_ms": 30.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": base + 15, "latency_ms": None, "timeout": True, "probe_method": "tcp"},
        ])
        stats = storage.get_range_stats(tid, start=now - 30 * 86400, end=now)
        assert stats["sample_count"] == 4
        assert stats["latency_count"] == 3
        assert stats["avg_latency_ms"] == 20.0
        assert stats["packet_loss_pct"] == 25.0
        assert stats["tiers_used"] == ["raw"]

    def test_range_stats_pinned_day_uses_surviving_raw(self, storage):
        """A pinned day keeps its raw rows, so its buckets must be skipped."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        midday = _local_midday(now - 10 * 86400)
        storage.pin_day(datetime.fromtimestamp(midday).strftime("%Y-%m-%d"))
        storage.save_samples([
            {"target_id": tid, "timestamp": midday, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": midday + 10, "latency_ms": 20.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": midday + 20, "latency_ms": 30.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": midday + 30, "latency_ms": None, "timeout": True, "probe_method": "tcp"},
        ])
        # aggregate() rewrites the buckets of a pinned day every cycle while
        # its raw rows survive, so both representations exist side by side.
        _save_buckets(storage, tid, 60, [
            {"bucket_start": midday, "avg_latency_ms": 20.0, "min_latency_ms": 10.0,
             "max_latency_ms": 30.0, "p95_latency_ms": 30.0,
             "packet_loss_pct": 25.0, "sample_count": 4},
        ])
        stats = storage.get_range_stats(tid, start=midday - 600, end=midday + 600)
        assert stats["sample_count"] == 4
        assert stats["latency_count"] == 3
        assert stats["avg_latency_ms"] == 20.0
        assert stats["min_latency_ms"] == 10.0
        assert stats["max_latency_ms"] == 30.0
        assert stats["packet_loss_pct"] == 25.0
        assert stats["p95_latency_ms"] == 30.0
        assert stats["tiers_used"] == ["raw"]

    def test_range_stats_pinned_day_not_double_counted(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        midday = _local_midday(now - 10 * 86400)
        storage.pin_day(datetime.fromtimestamp(midday).strftime("%Y-%m-%d"))
        samples = [
            {"target_id": tid, "timestamp": midday + i * 10, "latency_ms": 20.0,
             "timeout": False, "probe_method": "tcp"}
            for i in range(4)
        ]
        samples.extend([
            {"target_id": tid, "timestamp": now - 20, "latency_ms": 20.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now - 10, "latency_ms": 20.0, "timeout": False, "probe_method": "tcp"},
        ])
        storage.save_samples(samples)
        _save_buckets(storage, tid, 60, [
            {"bucket_start": midday, "avg_latency_ms": 20.0, "min_latency_ms": 20.0,
             "max_latency_ms": 20.0, "p95_latency_ms": 20.0,
             "packet_loss_pct": 0.0, "sample_count": 4},
            {"bucket_start": _local_midday(now - 9 * 86400), "avg_latency_ms": 20.0,
             "min_latency_ms": 20.0, "max_latency_ms": 20.0, "p95_latency_ms": 20.0,
             "packet_loss_pct": 0.0, "sample_count": 50},
        ])
        stats = storage.get_range_stats(tid, start=now - 30 * 86400, end=now)
        # 2 recent raw + 4 pinned raw + 50 from the one bucket that is not pinned
        assert stats["sample_count"] == 56
        assert stats["latency_count"] == 56
        assert stats["tiers_used"] == ["raw", "1min"]

    def test_range_stats_unpinned_day_with_surviving_raw(self, storage):
        """Un-pinning leaves raw in place until the next aggregate() runs."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        midday = _local_midday(now - 10 * 86400)
        date = datetime.fromtimestamp(midday).strftime("%Y-%m-%d")
        storage.pin_day(date)
        storage.save_samples([
            {"target_id": tid, "timestamp": midday + i, "latency_ms": 20.0,
             "timeout": False, "probe_method": "tcp"}
            for i in range(20)
        ])
        _save_buckets(storage, tid, 60, [
            {"bucket_start": midday, "avg_latency_ms": 20.0, "min_latency_ms": 20.0,
             "max_latency_ms": 20.0, "p95_latency_ms": 20.0,
             "packet_loss_pct": 0.0, "sample_count": 20},
        ])
        storage.unpin_day(date)
        stats = storage.get_range_stats(tid, start=midday - 600, end=midday + 600)
        assert stats["sample_count"] == 20
        assert stats["latency_count"] == 20
        assert stats["tiers_used"] == ["raw"]

    def test_range_stats_pinned_date_without_surviving_raw(self, storage):
        """Pinning a date whose raw was already pruned must not blank it out."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        midday = _local_midday(now - 10 * 86400)
        _save_buckets(storage, tid, 60, [
            {"bucket_start": midday, "avg_latency_ms": 20.0, "min_latency_ms": 20.0,
             "max_latency_ms": 20.0, "p95_latency_ms": 20.0,
             "packet_loss_pct": 0.0, "sample_count": 60},
        ])
        storage.pin_day(datetime.fromtimestamp(midday).strftime("%Y-%m-%d"))
        stats = storage.get_range_stats(tid, start=midday - 600, end=midday + 600)
        assert stats["sample_count"] == 60
        assert stats["avg_latency_ms"] == 20.0
        assert stats["tiers_used"] == ["1min"]

    def test_range_stats_ignores_latency_of_null_avg_bucket(self, storage):
        """A bucket without an average must not inflate the latency count."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        base = ((now - 10 * 86400) // 60) * 60
        _save_buckets(storage, tid, 60, [
            {"bucket_start": base, "avg_latency_ms": None, "min_latency_ms": None,
             "max_latency_ms": None, "p95_latency_ms": None,
             "packet_loss_pct": 50.0, "sample_count": 100},
            {"bucket_start": base + 60, "avg_latency_ms": 20.0, "min_latency_ms": 20.0,
             "max_latency_ms": 20.0, "p95_latency_ms": 20.0,
             "packet_loss_pct": 0.0, "sample_count": 10},
        ])
        stats = storage.get_range_stats(tid, start=base, end=base + 120)
        assert stats["sample_count"] == 110
        assert stats["latency_count"] == 10
        assert stats["avg_latency_ms"] == 20.0


class TestOutages:
    def test_derive_outages(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        # 5 consecutive timeouts = 1 outage
        samples = [
            {"target_id": tid, "timestamp": now - 50, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ]
        for i in range(5):
            samples.append({
                "target_id": tid, "timestamp": now - 40 + (i * 5),
                "latency_ms": None, "timeout": True, "probe_method": "tcp",
            })
        samples.append(
            {"target_id": tid, "timestamp": now, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        )
        storage.save_samples(samples)
        outages = storage.get_outages(tid, threshold=5)
        assert len(outages) == 1
        assert outages[0]["timeout_count"] == 5

    def test_no_outage_below_threshold(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        samples = [
            {"target_id": tid, "timestamp": now - 20, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now - 15, "latency_ms": None, "timeout": True, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now - 10, "latency_ms": None, "timeout": True, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now - 5, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ]
        storage.save_samples(samples)
        outages = storage.get_outages(tid, threshold=5)
        assert len(outages) == 0

    def test_outages_from_aggregated_buckets(self, storage):
        """Fully-lost bucket runs older than the raw retention become outages."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        base = ((now - 10 * 86400) // 60) * 60
        _save_buckets(storage, tid, 60, [
            {"bucket_start": base, "packet_loss_pct": 100.0, "sample_count": 6},
            {"bucket_start": base + 60, "packet_loss_pct": 100.0, "sample_count": 6},
            {"bucket_start": base + 120, "avg_latency_ms": 10.0, "min_latency_ms": 10.0,
             "max_latency_ms": 10.0, "p95_latency_ms": 10.0,
             "packet_loss_pct": 0.0, "sample_count": 60},
            # 100% loss but too few samples to reach the threshold
            {"bucket_start": base + 180, "packet_loss_pct": 100.0, "sample_count": 3},
            # partial loss never counts as an outage
            {"bucket_start": base + 240, "avg_latency_ms": 10.0, "min_latency_ms": 10.0,
             "max_latency_ms": 10.0, "p95_latency_ms": 10.0,
             "packet_loss_pct": 50.0, "sample_count": 60},
        ])
        outages = storage.get_outages(tid, threshold=5, start=base - 60, end=base + 300)
        assert len(outages) == 1
        assert outages[0]["start"] == base
        assert outages[0]["end"] == base + 120
        assert outages[0]["duration_seconds"] == 120.0
        assert outages[0]["timeout_count"] == 12
        assert outages[0]["approximate"] is True

    def test_outages_combine_raw_and_buckets_sorted(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        base = ((now - 10 * 86400) // 60) * 60
        _save_buckets(storage, tid, 60, [
            {"bucket_start": base, "packet_loss_pct": 100.0, "sample_count": 6},
        ])
        samples = [{"target_id": tid, "timestamp": now - 30, "latency_ms": 10.0,
                    "timeout": False, "probe_method": "tcp"}]
        for i in range(6):
            samples.append({
                "target_id": tid, "timestamp": now - 25 + i,
                "latency_ms": None, "timeout": True, "probe_method": "tcp",
            })
        samples.append({"target_id": tid, "timestamp": now - 5, "latency_ms": 10.0,
                        "timeout": False, "probe_method": "tcp"})
        storage.save_samples(samples)

        outages = storage.get_outages(tid, threshold=5, start=base - 60, end=now)
        assert len(outages) == 2
        assert outages[0]["start"] < outages[1]["start"]
        assert outages[0]["approximate"] is True
        assert outages[0]["timeout_count"] == 6
        # the recent outage still comes from the exact raw path
        assert "approximate" not in outages[1]
        assert outages[1]["timeout_count"] == 6

    def test_outages_from_raw_older_than_the_raw_tier(self, storage):
        """Raw not yet folded into buckets must still yield exact outages."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        base = now - 10 * 86400
        samples = [{"target_id": tid, "timestamp": base, "latency_ms": 10.0,
                    "timeout": False, "probe_method": "tcp"}]
        for i in range(6):
            samples.append({
                "target_id": tid, "timestamp": base + 5 + i * 5,
                "latency_ms": None, "timeout": True, "probe_method": "tcp",
            })
        samples.append({"target_id": tid, "timestamp": base + 40, "latency_ms": 10.0,
                        "timeout": False, "probe_method": "tcp"})
        storage.save_samples(samples)
        outages = storage.get_outages(tid, threshold=5, start=now - 30 * 86400, end=now)
        assert len(outages) == 1
        assert outages[0]["timeout_count"] == 6
        assert outages[0]["end"] == base + 40
        assert "approximate" not in outages[0]

    def test_outage_straddling_the_raw_boundary_survives_the_threshold(self, storage):
        """Neither half reaches the threshold alone, the merged outage does."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        boundary = ((now - storage._TIER_RAW_MAX_AGE) // 60) * 60
        # older half already aggregated: 4 timeouts in a fully lost bucket
        _save_buckets(storage, tid, 60, [
            {"bucket_start": boundary - 60, "packet_loss_pct": 100.0, "sample_count": 4},
        ])
        samples = [
            {"target_id": tid, "timestamp": boundary + 5 + i * 5,
             "latency_ms": None, "timeout": True, "probe_method": "tcp"}
            for i in range(4)
        ]
        samples.append({"target_id": tid, "timestamp": boundary + 25, "latency_ms": 10.0,
                        "timeout": False, "probe_method": "tcp"})
        storage.save_samples(samples)
        outages = storage.get_outages(tid, threshold=5, start=boundary - 120, end=now)
        assert len(outages) == 1
        assert outages[0]["start"] == boundary - 60
        assert outages[0]["end"] == boundary + 25
        assert outages[0]["timeout_count"] == 8
        assert outages[0]["approximate"] is True
        assert outages[0]["duration_seconds"] == 85.0

    def test_outage_not_merged_with_its_own_surviving_raw(self, storage):
        """An un-pinned day's bucket must not be added to the raw it describes."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        midday = _local_midday(now - 10 * 86400)
        date = datetime.fromtimestamp(midday).strftime("%Y-%m-%d")
        storage.pin_day(date)
        storage.save_samples([
            {"target_id": tid, "timestamp": midday + i, "latency_ms": None,
             "timeout": True, "probe_method": "tcp"}
            for i in range(20)
        ])
        _save_buckets(storage, tid, 60, [
            {"bucket_start": midday, "packet_loss_pct": 100.0, "sample_count": 20},
        ])
        storage.unpin_day(date)
        outages = storage.get_outages(tid, threshold=5, start=midday - 600, end=midday + 600)
        assert len(outages) == 1
        assert outages[0]["timeout_count"] == 20
        assert "approximate" not in outages[0]

    def test_outages_from_pinned_date_without_surviving_raw(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        midday = _local_midday(now - 10 * 86400)
        _save_buckets(storage, tid, 60, [
            {"bucket_start": midday, "packet_loss_pct": 100.0, "sample_count": 60},
        ])
        storage.pin_day(datetime.fromtimestamp(midday).strftime("%Y-%m-%d"))
        outages = storage.get_outages(tid, threshold=5, start=midday - 600, end=midday + 600)
        assert len(outages) == 1
        assert outages[0]["timeout_count"] == 60
        assert outages[0]["approximate"] is True

    def test_boundary_merge_does_not_chain_into_the_next_outage(self, storage):
        """A merged run must not swallow a second outage further along."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        boundary = ((now - storage._TIER_RAW_MAX_AGE) // 60) * 60
        _save_buckets(storage, tid, 60, [
            {"bucket_start": boundary - 60, "packet_loss_pct": 100.0, "sample_count": 4},
        ])
        samples = [
            {"target_id": tid, "timestamp": boundary + 5 + i * 5,
             "latency_ms": None, "timeout": True, "probe_method": "tcp"}
            for i in range(4)
        ]
        # 30s of clean samples separate the two outages
        samples.extend([
            {"target_id": tid, "timestamp": boundary + 25 + i * 5, "latency_ms": 10.0,
             "timeout": False, "probe_method": "tcp"}
            for i in range(7)
        ])
        samples.extend([
            {"target_id": tid, "timestamp": boundary + 60 + i * 5,
             "latency_ms": None, "timeout": True, "probe_method": "tcp"}
            for i in range(6)
        ])
        samples.append({"target_id": tid, "timestamp": boundary + 90, "latency_ms": 10.0,
                        "timeout": False, "probe_method": "tcp"})
        storage.save_samples(samples)
        outages = storage.get_outages(tid, threshold=5, start=boundary - 120, end=now)
        assert len(outages) == 2
        assert [o["timeout_count"] for o in outages] == [8, 6]
        assert outages[0]["approximate"] is True
        assert "approximate" not in outages[1]

    def test_ongoing_outage_survives_an_end_behind_the_server_clock(self, storage):
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        samples = [{"target_id": tid, "timestamp": now - 30, "latency_ms": 10.0,
                    "timeout": False, "probe_method": "tcp"}]
        for i in range(5):
            samples.append({
                "target_id": tid, "timestamp": now - 25 + i * 5,
                "latency_ms": None, "timeout": True, "probe_method": "tcp",
            })
        storage.save_samples(samples)
        outages = storage.get_outages(tid, threshold=5, start=now - 30 * 86400, end=now - 3)
        assert len(outages) == 1
        assert outages[0]["end"] is None
        assert outages[0]["timeout_count"] == 5

    def test_historical_window_ending_mid_outage_stays_open(self, storage):
        """Same semantics as the raw-only path: rows ending mid-run stay open."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        base = now - 10 * 86400
        samples = [{"target_id": tid, "timestamp": base, "latency_ms": 10.0,
                    "timeout": False, "probe_method": "tcp"}]
        for i in range(6):
            samples.append({
                "target_id": tid, "timestamp": base + 5 + i * 5,
                "latency_ms": None, "timeout": True, "probe_method": "tcp",
            })
        storage.save_samples(samples)
        outages = storage.get_outages(tid, threshold=5, start=base - 60, end=base + 100)
        assert len(outages) == 1
        assert outages[0]["end"] is None
        assert outages[0]["duration_seconds"] == 25.0

    def test_outages_do_not_merge_across_a_tier_boundary(self, storage):
        """60s and 300s runs describe different resolutions and stay separate."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        boundary = now - storage._TIER_60S_MAX_AGE
        _save_buckets(storage, tid, 300, [
            {"bucket_start": boundary - 300, "packet_loss_pct": 100.0, "sample_count": 6},
        ])
        _save_buckets(storage, tid, 60, [
            {"bucket_start": boundary + 60, "packet_loss_pct": 100.0, "sample_count": 6},
        ])
        outages = storage.get_outages(tid, threshold=5)
        assert len(outages) == 2
        assert [o["timeout_count"] for o in outages] == [6, 6]
        assert outages[0]["start"] == boundary - 300
        assert outages[1]["start"] == boundary + 60


class TestAggregation:
    def test_aggregated_table_exists(self, storage):
        """The aggregated table should be created on init."""
        with storage._connect() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='connection_samples_aggregated'"
            ).fetchone()
            assert row is not None

    def test_aggregate_raw_to_60s(self, storage):
        """Raw samples older than cutoff should be aggregated into 60s buckets."""
        tid = storage.create_target("Test", "1.1.1.1")
        base = 1700000000.0
        samples = [
            {"target_id": tid, "timestamp": base + 5, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": base + 10, "latency_ms": 20.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": base + 15, "latency_ms": 30.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": base + 20, "latency_ms": None, "timeout": True, "probe_method": "tcp"},
        ]
        storage.save_samples(samples)
        storage.aggregate_raw_to_buckets(tid, cutoff=base + 100, bucket_seconds=60)
        raw = storage.get_samples(tid)
        assert len(raw) == 0
        agg = storage.get_aggregated_samples(tid, bucket_seconds=60)
        assert len(agg) == 1
        bucket = agg[0]
        assert bucket["sample_count"] == 4
        assert abs(bucket["avg_latency_ms"] - 20.0) < 0.01
        assert bucket["min_latency_ms"] == 10.0
        assert bucket["max_latency_ms"] == 30.0
        assert abs(bucket["packet_loss_pct"] - 25.0) < 0.01
        assert bucket["p95_latency_ms"] is not None

    def test_aggregate_all_timeout_bucket(self, storage):
        """A bucket with only timeouts should have null latencies and 100% loss."""
        tid = storage.create_target("Test", "1.1.1.1")
        base = 1700000000.0
        samples = [
            {"target_id": tid, "timestamp": base + 5, "latency_ms": None, "timeout": True, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": base + 10, "latency_ms": None, "timeout": True, "probe_method": "tcp"},
        ]
        storage.save_samples(samples)
        storage.aggregate_raw_to_buckets(tid, cutoff=base + 100, bucket_seconds=60)
        agg = storage.get_aggregated_samples(tid, bucket_seconds=60)
        assert len(agg) == 1
        bucket = agg[0]
        assert bucket["avg_latency_ms"] is None
        assert bucket["min_latency_ms"] is None
        assert bucket["max_latency_ms"] is None
        assert bucket["p95_latency_ms"] is None
        assert bucket["packet_loss_pct"] == 100.0
        assert bucket["sample_count"] == 2

    def test_aggregate_creates_multiple_buckets(self, storage):
        """Samples spanning multiple 60s windows should create separate buckets."""
        tid = storage.create_target("Test", "1.1.1.1")
        base = 1700000000.0
        samples = [
            {"target_id": tid, "timestamp": base + 5, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": base + 65, "latency_ms": 50.0, "timeout": False, "probe_method": "tcp"},
        ]
        storage.save_samples(samples)
        created = storage.aggregate_raw_to_buckets(tid, cutoff=base + 200, bucket_seconds=60)
        assert created == 2
        agg = storage.get_aggregated_samples(tid, bucket_seconds=60)
        assert len(agg) == 2
        assert agg[0]["avg_latency_ms"] == 10.0
        assert agg[1]["avg_latency_ms"] == 50.0

    def test_aggregate_preserves_recent_samples(self, storage):
        """Samples newer than cutoff should not be aggregated."""
        tid = storage.create_target("Test", "1.1.1.1")
        base = 1700000000.0
        samples = [
            {"target_id": tid, "timestamp": base + 5, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": base + 100, "latency_ms": 20.0, "timeout": False, "probe_method": "tcp"},
        ]
        storage.save_samples(samples)
        storage.aggregate_raw_to_buckets(tid, cutoff=base + 50, bucket_seconds=60)
        raw = storage.get_samples(tid)
        assert len(raw) == 1
        assert raw[0]["latency_ms"] == 20.0
        agg = storage.get_aggregated_samples(tid, bucket_seconds=60)
        assert len(agg) == 1

    def test_aggregate_single_sample_bucket(self, storage):
        """A bucket with exactly one sample should produce correct aggregates."""
        tid = storage.create_target("Test", "1.1.1.1")
        base = 1700000000.0
        storage.save_samples([
            {"target_id": tid, "timestamp": base + 5, "latency_ms": 42.0, "timeout": False, "probe_method": "tcp"},
        ])
        storage.aggregate_raw_to_buckets(tid, cutoff=base + 100, bucket_seconds=60)
        agg = storage.get_aggregated_samples(tid, bucket_seconds=60)
        assert len(agg) == 1
        bucket = agg[0]
        assert bucket["sample_count"] == 1
        assert bucket["avg_latency_ms"] == 42.0
        assert bucket["min_latency_ms"] == 42.0
        assert bucket["max_latency_ms"] == 42.0
        assert bucket["p95_latency_ms"] == 42.0
        assert bucket["packet_loss_pct"] == 0.0

    def test_aggregate_empty_range_is_noop(self, storage):
        """Aggregation with no old-enough samples should be a no-op."""
        tid = storage.create_target("Test", "1.1.1.1")
        base = 1700000000.0
        storage.save_samples([
            {"target_id": tid, "timestamp": base + 5, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])
        created = storage.aggregate_raw_to_buckets(tid, cutoff=base, bucket_seconds=60)
        assert created == 0
        assert len(storage.get_samples(tid)) == 1
        assert len(storage.get_aggregated_samples(tid, bucket_seconds=60)) == 0

    def test_get_aggregated_samples_empty(self, storage):
        """Querying aggregated samples with no data returns empty list."""
        tid = storage.create_target("Test", "1.1.1.1")
        assert storage.get_aggregated_samples(tid, bucket_seconds=60) == []

    def test_reaggregate_60s_to_300s(self, storage):
        """60s buckets older than cutoff should be re-aggregated into 300s buckets."""
        tid = storage.create_target("Test", "1.1.1.1")
        # Align base to a 300s boundary so all 5 x 60s buckets land in one window
        base = (1700000000 // 300) * 300  # 1699999800.0
        # Insert 5 x 60s buckets (covering one 300s window)
        for i in range(5):
            with storage._connect() as conn:
                conn.execute(
                    """INSERT INTO connection_samples_aggregated
                       (target_id, bucket_start, bucket_seconds,
                        avg_latency_ms, min_latency_ms, max_latency_ms,
                        p95_latency_ms, packet_loss_pct, sample_count)
                       VALUES (?, ?, 60, ?, ?, ?, ?, ?, ?)""",
                    (tid, base + i * 60, (i + 1) * 10.0, (i + 1) * 5.0,
                     (i + 1) * 20.0, (i + 1) * 18.0, 0.0, 12),
                )

        storage.reaggregate_buckets(tid, cutoff=base + 400,
                                     source_seconds=60, target_seconds=300)

        # 60s buckets should be deleted
        agg_60 = storage.get_aggregated_samples(tid, bucket_seconds=60)
        assert len(agg_60) == 0

        # 300s bucket should exist
        agg_300 = storage.get_aggregated_samples(tid, bucket_seconds=300)
        assert len(agg_300) == 1
        bucket = agg_300[0]
        assert bucket["sample_count"] == 60  # 5 * 12
        assert bucket["min_latency_ms"] == 5.0   # min of all min values
        assert bucket["max_latency_ms"] == 100.0  # max of all max values
        assert bucket["p95_latency_ms"] == 90.0   # max of all p95 values

    def test_reaggregate_all_timeout_sources(self, storage):
        """Re-aggregation of all-timeout buckets should produce null latencies."""
        tid = storage.create_target("Test", "1.1.1.1")
        # Align base to a 300s boundary so all 3 x 60s buckets land in one window
        base = (1700000000 // 300) * 300  # 1699999800.0
        with storage._connect() as conn:
            for i in range(3):
                conn.execute(
                    """INSERT INTO connection_samples_aggregated
                       (target_id, bucket_start, bucket_seconds,
                        avg_latency_ms, min_latency_ms, max_latency_ms,
                        p95_latency_ms, packet_loss_pct, sample_count)
                       VALUES (?, ?, 60, NULL, NULL, NULL, NULL, 100.0, 10)""",
                    (tid, base + i * 60),
                )

        storage.reaggregate_buckets(tid, cutoff=base + 300,
                                     source_seconds=60, target_seconds=300)
        agg = storage.get_aggregated_samples(tid, bucket_seconds=300)
        assert len(agg) == 1
        assert agg[0]["avg_latency_ms"] is None
        assert agg[0]["packet_loss_pct"] == 100.0
        assert agg[0]["sample_count"] == 30

    def test_aggregate_full_cascade(self, storage):
        """aggregate() should cascade: raw -> 60s -> 300s -> 3600s."""
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()

        # Insert raw samples at different ages
        samples = []
        # 8 days ago (should become 60s buckets)
        for i in range(10):
            samples.append({
                "target_id": tid,
                "timestamp": now - 8 * 86400 + i * 5,
                "latency_ms": 10.0 + i,
                "timeout": False,
                "probe_method": "tcp",
            })
        # 35 days ago (should cascade to 300s)
        for i in range(10):
            samples.append({
                "target_id": tid,
                "timestamp": now - 35 * 86400 + i * 5,
                "latency_ms": 20.0 + i,
                "timeout": False,
                "probe_method": "tcp",
            })
        # 100 days ago (should cascade to 3600s)
        for i in range(10):
            samples.append({
                "target_id": tid,
                "timestamp": now - 100 * 86400 + i * 5,
                "latency_ms": 30.0 + i,
                "timeout": False,
                "probe_method": "tcp",
            })
        # Recent (should stay raw)
        samples.append({
            "target_id": tid,
            "timestamp": now - 60,
            "latency_ms": 5.0,
            "timeout": False,
            "probe_method": "tcp",
        })
        storage.save_samples(samples)

        storage.aggregate()

        # Recent raw sample preserved
        raw = storage.get_samples(tid)
        assert len(raw) == 1
        assert raw[0]["latency_ms"] == 5.0

        # 8-day-old data: should be in 60s buckets
        agg_60 = storage.get_aggregated_samples(
            tid, bucket_seconds=60,
            start=now - 9 * 86400, end=now - 7 * 86400,
        )
        assert len(agg_60) >= 1

        # 35-day-old data: should have cascaded through 60s to 300s
        agg_300 = storage.get_aggregated_samples(
            tid, bucket_seconds=300,
            start=now - 36 * 86400, end=now - 30 * 86400,
        )
        assert len(agg_300) >= 1

        # 100-day-old data: should have cascaded through 60s -> 300s -> 3600s
        agg_3600 = storage.get_aggregated_samples(
            tid, bucket_seconds=3600,
            start=now - 101 * 86400, end=now - 90 * 86400,
        )
        assert len(agg_3600) >= 1
