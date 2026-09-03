"""Tests for Connection Monitor API routes."""

import csv
import io
import math
import os
import random
import subprocess
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest

from app.modules.connection_monitor import routes as cm_routes
from app.modules.connection_monitor.routes import bp
from app.modules.connection_monitor.probe import ProbeEngine
from app.modules.connection_monitor.storage import ConnectionMonitorStorage
from app.app_factory import create_app
from app.config import ConfigManager
from app.runtime import get_runtime


@pytest.fixture
def app(tmp_path):
    app = create_app(config_manager=ConfigManager(str(tmp_path / "config")), environ={}, testing=True)
    app.register_blueprint(bp)

    db_path = str(tmp_path / "test_cm.db")
    storage = ConnectionMonitorStorage(db_path)

    mock_probe = MagicMock()
    mock_probe.capability_info.return_value = {"method": "tcp", "reason": "no ICMP permission"}

    with patch("app.modules.connection_monitor.routes._get_cm_storage", return_value=storage), \
         patch("app.modules.connection_monitor.routes._get_probe_engine", return_value=mock_probe), \
         patch("app.modules.connection_monitor.routes._get_tz", return_value="UTC"):
        yield app, storage


@pytest.fixture
def client(app):
    flask_app, storage = app
    return flask_app.test_client(), storage


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


def _auth_session(c, marker_source=None):
    """Set authenticated session for protected routes."""
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        if marker_source:
            from app.web import _admin_session_marker
            sess["auth_marker"] = _admin_session_marker(marker_source)


class TestTargetsAPI:
    def test_get_empty_targets(self, client):
        c, _ = client
        resp = c.get("/api/connection-monitor/targets")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_create_target(self, client):
        c, _ = client
        _auth_session(c)
        resp = c.post(
            "/api/connection-monitor/targets",
            json={"label": "Test", "host": "1.1.1.1"},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["id"] == 1

    def test_create_target_defaults_to_configured_interval(self, client):
        c, storage = client
        _auth_session(c)
        cfg = MagicMock()
        cfg.get.side_effect = lambda key, default=None: (
            1000 if key == "connection_monitor_poll_interval_ms" else default
        )
        with patch("app.modules.connection_monitor.routes.get_config_manager",
                   return_value=cfg):
            resp = c.post(
                "/api/connection-monitor/targets",
                json={"label": "Test", "host": "1.1.1.1"},
            )
        assert resp.status_code == 201
        assert storage.get_targets()[0]["poll_interval_ms"] == 1000

    def test_create_target_explicit_interval_wins(self, client):
        c, storage = client
        _auth_session(c)
        resp = c.post(
            "/api/connection-monitor/targets",
            json={"label": "Test", "host": "1.1.1.1", "poll_interval_ms": 3000},
        )
        assert resp.status_code == 201
        assert storage.get_targets()[0]["poll_interval_ms"] == 3000

    def test_create_target_without_host_is_disabled(self, client):
        c, storage = client
        _auth_session(c)
        resp = c.post(
            "/api/connection-monitor/targets",
            json={"label": "New target"},
        )
        assert resp.status_code == 201
        target = storage.get_target(resp.get_json()["id"])
        assert not target["enabled"]

    def test_create_target_with_host_is_enabled(self, client):
        c, storage = client
        _auth_session(c)
        resp = c.post(
            "/api/connection-monitor/targets",
            json={"label": "Test", "host": "1.1.1.1"},
        )
        assert resp.status_code == 201
        target = storage.get_target(resp.get_json()["id"])
        assert target["enabled"]

    def test_update_host_auto_enables_target(self, client):
        c, storage = client
        _auth_session(c)
        # Create disabled target (no host)
        resp = c.post(
            "/api/connection-monitor/targets",
            json={"label": "New target"},
        )
        tid = resp.get_json()["id"]
        assert not storage.get_target(tid)["enabled"]
        # Update with host - should auto-enable
        resp = c.put(
            f"/api/connection-monitor/targets/{tid}",
            json={"host": "8.8.8.8"},
        )
        assert resp.status_code == 200
        assert storage.get_target(tid)["enabled"]

    def test_update_target(self, client):
        c, storage = client
        storage.create_target("Test", "1.1.1.1")
        _auth_session(c)
        resp = c.put(
            "/api/connection-monitor/targets/1",
            json={"label": "Updated"},
        )
        assert resp.status_code == 200

    def test_delete_target(self, client):
        c, storage = client
        storage.create_target("Test", "1.1.1.1")
        _auth_session(c)
        resp = c.delete("/api/connection-monitor/targets/1")
        assert resp.status_code == 200


class TestSamplesAPI:
    def test_get_samples(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        storage.save_samples([
            {"target_id": tid, "timestamp": time.time(), "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])
        resp = c.get(f"/api/connection-monitor/samples/{tid}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["samples"]) == 1

    def test_get_samples_with_time_range(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        storage.save_samples([
            {"target_id": tid, "timestamp": now - 200, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now - 50, "latency_ms": 20.0, "timeout": False, "probe_method": "tcp"},
        ])
        resp = c.get(f"/api/connection-monitor/samples/{tid}?start={now - 100}")
        data = resp.get_json()
        assert len(data["samples"]) == 1


class TestSamplesResolution:
    def test_raw_returns_envelope_format(self, client):
        """Samples endpoint should return {meta, samples} envelope."""
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        storage.save_samples([
            {"target_id": tid, "timestamp": now, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])
        resp = c.get(f"/api/connection-monitor/samples/{tid}?start={now - 60}&end={now + 60}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "meta" in data
        assert "samples" in data
        assert data["meta"]["resolution"] == "raw"
        assert data["meta"]["bucket_seconds"] is None
        assert data["meta"]["blended"] is False
        assert data["meta"]["mixed"] is False
        assert data["meta"]["tiers_used"] == ["raw"]
        s = data["samples"][0]
        assert "latency_ms" in s
        assert "packet_loss_pct" in s
        assert "sample_count" in s
        assert s["sample_count"] == 1
        assert s["bucket_seconds"] is None
        assert s["min_latency_ms"] is None
        assert s["max_latency_ms"] is None
        assert s["p95_latency_ms"] is None
        assert "timeout" not in s

    def test_raw_timeout_has_100_loss(self, client):
        """Raw timeout samples should have packet_loss_pct=100.0."""
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        storage.save_samples([
            {"target_id": tid, "timestamp": now, "latency_ms": None, "timeout": True, "probe_method": "tcp"},
        ])
        resp = c.get(f"/api/connection-monitor/samples/{tid}?start={now - 60}&end={now + 60}")
        data = resp.get_json()
        s = data["samples"][0]
        assert s["packet_loss_pct"] == 100.0
        assert s["latency_ms"] is None

    @pytest.mark.parametrize(
        ("resolution", "bucket_seconds"),
        (("1min", 60), ("5min", 300), ("1hr", 3600)),
    )
    def test_forced_resolution_reports_per_sample_tier_width(
        self, client, resolution, bucket_seconds
    ):
        """Explicit aggregate tiers expose the row's coverage on every sample."""
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        with storage._connect() as conn:
            conn.execute(
                """INSERT INTO connection_samples_aggregated
                   (target_id, bucket_start, bucket_seconds,
                    avg_latency_ms, min_latency_ms, max_latency_ms,
                    p95_latency_ms, packet_loss_pct, sample_count)
                   VALUES (?, ?, ?, 15.0, 10.0, 20.0, 18.0, 0.0, 12)""",
                (tid, now - 500, bucket_seconds),
            )
        resp = c.get(
            f"/api/connection-monitor/samples/{tid}?resolution={resolution}&start={now - 600}&end={now}"
        )
        data = resp.get_json()
        assert data["meta"]["resolution"] == resolution
        assert data["meta"]["bucket_seconds"] == bucket_seconds
        assert len(data["samples"]) == 1
        s = data["samples"][0]
        assert s["bucket_seconds"] == bucket_seconds
        assert s["min_latency_ms"] == 10.0
        assert s["max_latency_ms"] == 20.0
        assert s["sample_count"] == 12

    def test_auto_7d_range_is_blended(self, client):
        """A 7d range with auto resolution should blend raw + aggregated data."""
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        storage.save_samples([
            {"target_id": tid, "timestamp": now - 3600, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])
        with storage._connect() as conn:
            conn.execute(
                """INSERT INTO connection_samples_aggregated
                   (target_id, bucket_start, bucket_seconds,
                    avg_latency_ms, min_latency_ms, max_latency_ms,
                    p95_latency_ms, packet_loss_pct, sample_count)
                   VALUES (?, ?, 60, 15.0, 10.0, 20.0, 18.0, 0.0, 12)""",
                (tid, now - 8 * 86400),
            )
        start = now - 9 * 86400
        end = now
        resp = c.get(f"/api/connection-monitor/samples/{tid}?start={start}&end={end}")
        data = resp.get_json()
        assert data["meta"]["blended"] is True
        assert data["meta"]["mixed"] is True
        assert data["meta"]["tiers_used"] == ["raw", "1min"]
        assert len(data["samples"]) == 2
        assert data["samples"][0]["timestamp"] < data["samples"][1]["timestamp"]
        assert data["samples"][0]["min_latency_ms"] is not None
        assert data["samples"][1]["min_latency_ms"] is None
        assert [sample["bucket_seconds"] for sample in data["samples"]] == [60, None]

    def test_auto_90d_range_keeps_recent_raw_data(self, client):
        """A 90d range should still show current raw data even before older buckets exist."""
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        storage.save_samples([
            {"target_id": tid, "timestamp": now - 3600, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])
        resp = c.get(f"/api/connection-monitor/samples/{tid}?start={now - 90 * 86400}&end={now}")
        data = resp.get_json()
        assert data["meta"]["resolution"] == "5min"
        assert data["meta"]["blended"] is True
        assert data["meta"]["mixed"] is False
        assert data["meta"]["tiers_used"] == ["raw"]
        assert len(data["samples"]) == 1
        assert data["samples"][0]["latency_ms"] == 10.0
        assert data["samples"][0]["min_latency_ms"] is None

    def test_auto_90d_range_blends_raw_60s_and_300s(self, client):
        """A 90d range should combine recent raw data with 60s and 300s buckets."""
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        storage.save_samples([
            {"target_id": tid, "timestamp": now - 3600, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])
        with storage._connect() as conn:
            conn.execute(
                """INSERT INTO connection_samples_aggregated
                   (target_id, bucket_start, bucket_seconds,
                    avg_latency_ms, min_latency_ms, max_latency_ms,
                    p95_latency_ms, packet_loss_pct, sample_count)
                   VALUES (?, ?, 60, 15.0, 10.0, 20.0, 18.0, 0.0, 12)""",
                (tid, now - 14 * 86400),
            )
            conn.execute(
                """INSERT INTO connection_samples_aggregated
                   (target_id, bucket_start, bucket_seconds,
                    avg_latency_ms, min_latency_ms, max_latency_ms,
                    p95_latency_ms, packet_loss_pct, sample_count)
                   VALUES (?, ?, 300, 25.0, 20.0, 30.0, 28.0, 1.0, 60)""",
                (tid, now - 45 * 86400),
            )
        resp = c.get(f"/api/connection-monitor/samples/{tid}?start={now - 90 * 86400}&end={now}")
        data = resp.get_json()
        assert data["meta"]["resolution"] == "5min"
        assert data["meta"]["blended"] is True
        assert data["meta"]["mixed"] is True
        assert data["meta"]["tiers_used"] == ["raw", "1min", "5min"]
        assert len(data["samples"]) == 3
        assert data["samples"][0]["timestamp"] < data["samples"][1]["timestamp"] < data["samples"][2]["timestamp"]
        assert data["samples"][0]["min_latency_ms"] is not None
        assert data["samples"][1]["min_latency_ms"] is not None
        assert data["samples"][2]["min_latency_ms"] is None
        assert [sample["bucket_seconds"] for sample in data["samples"]] == [300, 60, None]

    def test_auto_without_explicit_range_stays_raw_only(self, client):
        """Without start/end, auto resolution should keep the legacy raw-only behavior."""
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        storage.save_samples([
            {"target_id": tid, "timestamp": now - 60, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])
        with storage._connect() as conn:
            conn.execute(
                """INSERT INTO connection_samples_aggregated
                   (target_id, bucket_start, bucket_seconds,
                    avg_latency_ms, min_latency_ms, max_latency_ms,
                    p95_latency_ms, packet_loss_pct, sample_count)
                   VALUES (?, ?, 60, 15.0, 10.0, 20.0, 18.0, 0.0, 12)""",
                (tid, now - 14 * 86400),
            )
        resp = c.get(f"/api/connection-monitor/samples/{tid}")
        data = resp.get_json()
        assert data["meta"]["resolution"] == "raw"
        assert data["meta"]["blended"] is False
        assert data["meta"]["mixed"] is False
        assert data["meta"]["tiers_used"] == ["raw"]
        assert len(data["samples"]) == 1
        assert data["samples"][0]["latency_ms"] == 10.0
        assert data["samples"][0]["min_latency_ms"] is None

    def test_auto_range_uses_exclusive_tier_boundaries(self, client):
        """Tier boundaries should keep near-cutoff samples in exactly one tier."""
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        raw_ts = now - 7 * 86400 + 5
        agg60_near_raw_ts = now - 7 * 86400 - 5
        agg60_near_300_ts = now - 30 * 86400 + 5
        agg300_ts = now - 30 * 86400 - 5
        storage.save_samples([
            {"target_id": tid, "timestamp": raw_ts, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])
        with storage._connect() as conn:
            conn.execute(
                """INSERT INTO connection_samples_aggregated
                   (target_id, bucket_start, bucket_seconds,
                    avg_latency_ms, min_latency_ms, max_latency_ms,
                    p95_latency_ms, packet_loss_pct, sample_count)
                   VALUES (?, ?, 60, 15.0, 10.0, 20.0, 18.0, 0.0, 12)""",
                (tid, agg60_near_raw_ts),
            )
            conn.execute(
                """INSERT INTO connection_samples_aggregated
                   (target_id, bucket_start, bucket_seconds,
                    avg_latency_ms, min_latency_ms, max_latency_ms,
                    p95_latency_ms, packet_loss_pct, sample_count)
                   VALUES (?, ?, 60, 17.0, 12.0, 22.0, 19.0, 0.0, 12)""",
                (tid, agg60_near_300_ts),
            )
            conn.execute(
                """INSERT INTO connection_samples_aggregated
                   (target_id, bucket_start, bucket_seconds,
                    avg_latency_ms, min_latency_ms, max_latency_ms,
                    p95_latency_ms, packet_loss_pct, sample_count)
                   VALUES (?, ?, 300, 25.0, 20.0, 30.0, 28.0, 1.0, 60)""",
                (tid, agg300_ts),
            )
        resp = c.get(f"/api/connection-monitor/samples/{tid}?start={now - 90 * 86400}&end={now}")
        data = resp.get_json()
        timestamps = [sample["timestamp"] for sample in data["samples"]]
        assert len(data["samples"]) == 4
        assert timestamps.count(raw_ts) == 1
        assert timestamps.count(agg60_near_raw_ts) == 1
        assert timestamps.count(agg60_near_300_ts) == 1
        assert timestamps.count(agg300_ts) == 1

    def test_get_samples_with_max_points(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        storage.save_samples([
            {
                "target_id": tid,
                "timestamp": now - (120 - i),
                "latency_ms": float(10 + (i % 5)),
                "timeout": i % 17 == 0,
                "probe_method": "tcp",
            }
            for i in range(120)
        ])
        resp = c.get(
            f"/api/connection-monitor/samples/{tid}?start={now - 120}&end={now}&limit=0&max_points=10"
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["samples"]) <= 10
        assert "sample_count" in data["samples"][0]
        assert "packet_loss_pct" in data["samples"][0]
        assert sum(sample["sample_count"] for sample in data["samples"]) == 120
        assert {sample["bucket_seconds"] for sample in data["samples"]} == {12}


def _frozen_clock(now):
    """Pin the tier boundaries so repeated requests split raw/aggregated alike."""
    return patch.object(cm_routes, "time", SimpleNamespace(time=lambda: now))


def _seed_raw_window(storage, tid, start, end, step, seed=4242):
    """Uneven raw traffic with duplicate latencies, timeouts and gaps."""
    rng = random.Random(seed)
    rows = []
    ts = start
    while ts < end:
        if rng.random() < 0.02:
            ts += rng.uniform(step, 20 * step)
            continue
        timeout = rng.random() < 0.06
        rows.append({
            "target_id": tid,
            "timestamp": ts,
            "latency_ms": None if timeout else rng.choice([9.5, 11.25, 11.25, 12.0, 48.75]),
            "timeout": timeout,
            "probe_method": "tcp",
        })
        ts += step * rng.uniform(0.4, 1.6)
    storage.save_samples(rows)
    return len(rows)


def _seed_aggregated(storage, tid, start, end, bucket_seconds, step):
    rows = 0
    with storage._connect() as conn:
        ts = start
        while ts < end:
            conn.execute(
                """INSERT OR REPLACE INTO connection_samples_aggregated
                   (target_id, bucket_start, bucket_seconds,
                    avg_latency_ms, min_latency_ms, max_latency_ms,
                    p95_latency_ms, packet_loss_pct, sample_count)
                   VALUES (?, ?, ?, 15.5, 9.0, 41.0, 38.0, 1.25, 12)""",
                (tid, ts, bucket_seconds),
            )
            ts += step
            rows += 1
    return rows


def _assert_payload_parity(c, tid, start, end, max_points, extra=""):
    """Assert the pre-bucketed payload equals the row-by-row one.

    The SQL pre-bucketing only engages for unlimited reads, so the very same
    request with a limit above the row count runs the untouched path and yields
    the reference payload.
    """
    url = (f"/api/connection-monitor/samples/{tid}?start={start!r}&end={end!r}"
           f"&max_points={max_points}{extra}")
    bucketed = c.get(url + "&limit=0").get_json()
    reference = c.get(url + "&limit=1000000").get_json()
    assert bucketed["meta"] == reference["meta"]
    assert len(bucketed["samples"]) == len(reference["samples"])
    for got, want in zip(bucketed["samples"], reference["samples"]):
        assert got.keys() == want.keys()
        for key, value in want.items():
            if key == "latency_ms" and value is not None:
                # The bucket mean now leaves SQLite as a compensated SUM()
                # instead of a running Python sum, so its last bits can move.
                assert got[key] == pytest.approx(value, rel=1e-12)
            else:
                assert got[key] == value
    return bucketed


class TestSamplesRawTierBucketing:
    """The raw tier is pre-bucketed in SQL without changing the payload."""

    def test_raw_range_matches_row_by_row_payload(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = float(int(time.time()))
        start, end = now - 7 * 86400, now
        rows = _seed_raw_window(storage, tid, start, end, step=60)
        assert rows > 1440
        with _frozen_clock(now):
            data = _assert_payload_parity(c, tid, start, end, 1440)
            with patch.object(storage, "get_samples_bucketed") as spy:
                c.get(f"/api/connection-monitor/samples/{tid}"
                      f"?start={start!r}&end={end!r}&limit=0&max_points=1440")
            assert spy.called
        assert data["meta"]["tiers_used"] == ["raw"]
        assert {s["bucket_seconds"] for s in data["samples"]} == {420}
        assert sum(s["sample_count"] for s in data["samples"]) == rows

    @pytest.mark.parametrize("max_points", (1000, 1440))
    def test_blended_range_matches_row_by_row_payload(self, client, max_points):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = float(int(time.time()))
        start, end = now - 30 * 86400, now
        boundary = now - 7 * 86400
        rows = _seed_raw_window(storage, tid, boundary, end, step=120)
        _seed_aggregated(storage, tid, start, boundary, 60, 900)
        # A bucket straddling the raw/60s boundary is the case where the two
        # tiers have to merge; max_points=1000 puts the boundary inside one.
        width = math.ceil((end - start) / max_points)
        assert ((boundary - start) % width == 0) == (max_points == 1440)
        with _frozen_clock(now):
            data = _assert_payload_parity(c, tid, start, end, max_points)
        assert data["meta"]["tiers_used"] == ["raw", "1min"]
        assert sum(s["sample_count"] for s in data["samples"]) > rows

    def test_range_without_raw_samples_matches(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = float(int(time.time()))
        start, end = now - 30 * 86400, now - 20 * 86400
        _seed_aggregated(storage, tid, start, end, 60, 300)
        with _frozen_clock(now):
            data = _assert_payload_parity(c, tid, start, end, 1440)
        assert data["meta"]["tiers_used"] == ["1min"]

    def test_sparse_raw_tier_stays_row_per_sample(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = float(int(time.time()))
        start, end = now - 7 * 86400, now
        storage.save_samples([
            {"target_id": tid, "timestamp": start + i * 3000.0, "latency_ms": 10.0 + i,
             "timeout": False, "probe_method": "tcp"}
            for i in range(200)
        ])
        with _frozen_clock(now):
            data = _assert_payload_parity(c, tid, start, end, 1440)
        # Fewer rows than max_points: the compressor never ran, so neither
        # should the pre-bucketing.
        assert len(data["samples"]) == 200
        assert {s["bucket_seconds"] for s in data["samples"]} == {None}

    def test_dense_raw_in_few_buckets_still_compresses_other_tiers(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = float(int(time.time()))
        start, end = now - 30 * 86400, now
        boundary = now - 7 * 86400
        storage.save_samples([
            {"target_id": tid, "timestamp": boundary + i * 1.5, "latency_ms": 10.0 + (i % 7),
             "timeout": i % 23 == 0, "probe_method": "tcp"}
            for i in range(2000)
        ])
        _seed_aggregated(storage, tid, start, start + 50 * 3600, 60, 3600)
        with _frozen_clock(now):
            data = _assert_payload_parity(c, tid, start, end, 1440)
        # The raw tier collapses to two buckets, so the payload looks small
        # even though the range holds far more rows than max_points.
        assert {s["bucket_seconds"] for s in data["samples"]} == {1800}

    def test_range_shorter_than_one_bucket_matches(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = float(int(time.time()))
        start, end = now - 60, now
        _seed_raw_window(storage, tid, start, end, step=2)
        with _frozen_clock(now):
            data = _assert_payload_parity(c, tid, start, end, 1)
        assert len(data["samples"]) == 1
        assert data["samples"][0]["timestamp"] == start

    def test_sub_two_second_grid_keeps_the_row_by_row_path(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = float(int(time.time()))
        start, end = now - 500, now
        rows = _seed_raw_window(storage, tid, start, end, step=0.5)
        assert rows > 600  # the compressor still runs, on 1s buckets
        with _frozen_clock(now), patch.object(storage, "get_samples_bucketed") as spy:
            data = _assert_payload_parity(c, tid, start, end, 600)
        assert not spy.called
        assert {s["bucket_seconds"] for s in data["samples"]} == {1}

    def test_p95_matches_on_a_bucket_of_duplicate_latencies(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = float(int(time.time()))
        start, end = now - 400, now
        latencies = [12.5] * 60 + [12.5] * 30 + [40.0] * 8 + [77.5] * 2
        storage.save_samples([
            {"target_id": tid, "timestamp": start + i, "latency_ms": latency,
             "timeout": False, "probe_method": "tcp"}
            for i, latency in enumerate(latencies)
        ])
        with _frozen_clock(now):
            data = _assert_payload_parity(c, tid, start, end, 2)
        # nearest-rank p95 over the first bucket: index floor(100 * 0.95) = 95
        assert data["samples"][0]["sample_count"] == 100
        assert data["samples"][0]["p95_latency_ms"] == 40.0

    def test_pinned_day_resolution_stays_row_per_sample(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = float(int(time.time()))
        start, end = now - 86400, now
        rows = _seed_raw_window(storage, tid, start, end, step=30)
        with patch.object(storage, "get_samples_bucketed") as spy:
            resp = c.get(f"/api/connection-monitor/samples/{tid}"
                         f"?resolution=raw&start={start!r}&end={end!r}&limit=0")
        assert not spy.called
        data = resp.get_json()
        assert len(data["samples"]) == rows
        assert {s["bucket_seconds"] for s in data["samples"]} == {None}


class TestPinnedDaysAPI:
    def test_list_pinned_days_empty(self, client):
        c, _ = client
        resp = c.get("/api/connection-monitor/pinned-days")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_pin_day(self, client):
        c, _ = client
        _auth_session(c)
        resp = c.post(
            "/api/connection-monitor/pinned-days",
            json={"date": "2026-03-10"},
        )
        assert resp.status_code == 201
        days = c.get("/api/connection-monitor/pinned-days").get_json()
        assert len(days) == 1
        assert days[0]["date"] == "2026-03-10"
        assert "utc_start" in days[0]
        assert "utc_end" in days[0]

    def test_pin_day_via_timestamp(self, client):
        """POST with timestamp instead of date derives date server-side."""
        c, _ = client
        _auth_session(c)
        from datetime import datetime, timezone
        # 2026-03-10 12:00:00 UTC
        ts = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        resp = c.post(
            "/api/connection-monitor/pinned-days",
            json={"timestamp": ts},
        )
        assert resp.status_code == 201
        days = c.get("/api/connection-monitor/pinned-days").get_json()
        assert len(days) == 1
        assert days[0]["date"] == "2026-03-10"

    def test_pin_day_with_label(self, client):
        c, _ = client
        _auth_session(c)
        resp = c.post(
            "/api/connection-monitor/pinned-days",
            json={"date": "2026-03-10", "label": "Outage"},
        )
        assert resp.status_code == 201
        days = c.get("/api/connection-monitor/pinned-days").get_json()
        assert days[0]["label"] == "Outage"

    def test_pin_day_invalid_date(self, client):
        c, _ = client
        _auth_session(c)
        resp = c.post(
            "/api/connection-monitor/pinned-days",
            json={"date": "not-a-date"},
        )
        assert resp.status_code == 400

    def test_pin_day_future_date(self, client):
        c, _ = client
        _auth_session(c)
        resp = c.post(
            "/api/connection-monitor/pinned-days",
            json={"date": "2099-01-01"},
        )
        assert resp.status_code == 400

    def test_pin_day_missing_date_and_timestamp(self, client):
        c, _ = client
        _auth_session(c)
        resp = c.post(
            "/api/connection-monitor/pinned-days",
            json={},
        )
        assert resp.status_code == 400

    def test_unpin_day(self, client):
        c, _ = client
        _auth_session(c)
        c.post("/api/connection-monitor/pinned-days", json={"date": "2026-03-10"})
        resp = c.delete("/api/connection-monitor/pinned-days/2026-03-10")
        assert resp.status_code == 200
        days = c.get("/api/connection-monitor/pinned-days").get_json()
        assert len(days) == 0

    def test_unpin_nonexistent(self, client):
        c, _ = client
        _auth_session(c)
        resp = c.delete("/api/connection-monitor/pinned-days/2026-01-01")
        assert resp.status_code == 404

    def test_pinned_day_older_than_7d_returns_raw(self, client):
        """Pinned day raw data should be served when resolution=raw, even beyond the 7d window."""
        c, storage = client
        _auth_session(c)
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        old_ts = now - 10 * 86400  # 10 days ago
        from datetime import datetime
        old_date = datetime.fromtimestamp(old_ts).strftime("%Y-%m-%d")
        storage.pin_day(old_date)
        storage.save_samples([
            {"target_id": tid, "timestamp": old_ts, "latency_ms": 42.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": old_ts + 5, "latency_ms": 43.0, "timeout": False, "probe_method": "tcp"},
        ])
        # Simulate what the JS does for pinned days: resolution=raw
        resp = c.get(
            f"/api/connection-monitor/samples/{tid}"
            f"?start={old_ts - 3600}&end={old_ts + 86400}&limit=0&resolution=raw"
        )
        data = resp.get_json()
        assert data["meta"]["resolution"] == "raw"
        assert len(data["samples"]) == 2
        assert data["samples"][0]["latency_ms"] == 42.0
        assert data["samples"][0]["sample_count"] == 1

    def test_pinned_day_older_than_7d_export_returns_raw(self, client):
        """CSV export of a pinned day should return raw samples."""
        c, storage = client
        _auth_session(c)
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        old_ts = now - 10 * 86400
        from datetime import datetime
        old_date = datetime.fromtimestamp(old_ts).strftime("%Y-%m-%d")
        storage.pin_day(old_date)
        storage.save_samples([
            {"target_id": tid, "timestamp": old_ts, "latency_ms": 42.0, "timeout": False, "probe_method": "tcp"},
        ])
        resp = c.get(f"/api/connection-monitor/export/{tid}?start={old_ts - 3600}&end={old_ts + 86400}&resolution=raw")
        assert resp.status_code == 200
        import csv, io
        rows = list(csv.reader(io.StringIO(resp.data.decode())))
        assert len(rows) == 2  # header + 1 data row
        assert "latency_ms" in rows[0]


class TestSummaryAPI:
    def test_get_summary(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        storage.save_samples([
            {"target_id": tid, "timestamp": now - 5, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])
        resp = c.get("/api/connection-monitor/summary")
        assert resp.status_code == 200

    def test_summary_is_empty_when_connection_monitor_is_disabled(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1", enabled=True)
        now = time.time()
        storage.save_samples([
            {"target_id": tid, "timestamp": now - 5, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])

        mock_cfg = MagicMock()
        mock_cfg.get.side_effect = lambda key, default=None: (
            False if key == "connection_monitor_enabled" else default
        )
        with patch("app.modules.connection_monitor.routes.get_config_manager", return_value=mock_cfg):
            resp = c.get("/api/connection-monitor/summary")

        assert resp.status_code == 200
        assert resp.get_json() == {}

    def test_get_range_stats(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        storage.save_samples([
            {"target_id": tid, "timestamp": now - 40, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now - 30, "latency_ms": 20.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now - 20, "latency_ms": 30.0, "timeout": False, "probe_method": "tcp"},
            {"target_id": tid, "timestamp": now - 10, "latency_ms": None, "timeout": True, "probe_method": "tcp"},
        ])
        resp = c.get(f"/api/connection-monitor/stats?start={now - 60}&end={now}")
        assert resp.status_code == 200
        data = resp.get_json()
        stats = data[str(tid)]
        assert stats["sample_count"] == 4
        assert stats["latency_count"] == 3
        assert stats["p95_latency_ms"] == 30.0
        assert stats["tiers_used"] == ["raw"]

    def test_get_range_stats_for_one_target(self, client):
        """target_id narrows the read without changing the payload shape."""
        c, storage = client
        now = time.time()
        first = storage.create_target("First", "1.1.1.1")
        second = storage.create_target("Second", "8.8.8.8")
        storage.save_samples([
            {"target_id": tid, "timestamp": now - 10, "latency_ms": 10.0,
             "timeout": False, "probe_method": "tcp"}
            for tid in (first, second)
        ])
        resp = c.get(
            f"/api/connection-monitor/stats?start={now - 60}&end={now}&target_id={second}"
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert list(data) == [str(second)]
        assert data[str(second)] == (
            c.get(f"/api/connection-monitor/stats?start={now - 60}&end={now}")
            .get_json()[str(second)]
        )

    def test_get_range_stats_for_an_unknown_target(self, client):
        c, storage = client
        now = time.time()
        storage.create_target("Test", "1.1.1.1")
        resp = c.get(
            f"/api/connection-monitor/stats?start={now - 60}&end={now}&target_id=999"
        )
        assert resp.status_code == 200
        assert resp.get_json() == {}

    def test_get_range_stats_reads_aggregated_tiers(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        base = ((now - 10 * 86400) // 60) * 60
        _save_buckets(storage, tid, 60, [
            {"bucket_start": base, "avg_latency_ms": 10.0, "min_latency_ms": 5.0,
             "max_latency_ms": 20.0, "p95_latency_ms": 18.0,
             "packet_loss_pct": 0.0, "sample_count": 60},
        ])
        resp = c.get(f"/api/connection-monitor/stats?start={base}&end={base + 60}")
        assert resp.status_code == 200
        stats = resp.get_json()[str(tid)]
        assert stats["sample_count"] == 60
        assert stats["avg_latency_ms"] == 10.0
        assert stats["p95_latency_ms"] == 18.0
        assert stats["tiers_used"] == ["1min"]


class TestOutagesAPI:
    def test_get_outages(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        samples = [{"target_id": tid, "timestamp": now - 10, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"}]
        for i in range(6):
            samples.append({"target_id": tid, "timestamp": now - 9 + i, "latency_ms": None, "timeout": True, "probe_method": "tcp"})
        samples.append({"target_id": tid, "timestamp": now, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"})
        storage.save_samples(samples)
        resp = c.get(f"/api/connection-monitor/outages/{tid}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) >= 1

    def test_get_outages_reads_aggregated_tiers(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        base = ((now - 10 * 86400) // 60) * 60
        _save_buckets(storage, tid, 60, [
            {"bucket_start": base, "packet_loss_pct": 100.0, "sample_count": 6},
        ])
        resp = c.get(
            f"/api/connection-monitor/outages/{tid}?start={base - 60}&end={base + 120}"
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["start"] == base
        assert data[0]["duration_seconds"] == 60.0
        assert data[0]["approximate"] is True


class TestExportAPI:
    def test_csv_export(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        storage.save_samples([
            {"target_id": tid, "timestamp": now, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])
        resp = c.get(f"/api/connection-monitor/export/{tid}")
        assert resp.status_code == 200
        assert "text/csv" in resp.content_type
        content = resp.data.decode()
        reader = csv.reader(io.StringIO(content))
        rows = list(reader)
        assert len(rows) == 2  # header + 1 data row

    def test_csv_export_aggregated(self, client):
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        with storage._connect() as conn:
            conn.execute(
                """INSERT INTO connection_samples_aggregated
                   (target_id, bucket_start, bucket_seconds,
                    avg_latency_ms, min_latency_ms, max_latency_ms,
                    p95_latency_ms, packet_loss_pct, sample_count)
                   VALUES (?, ?, 60, 15.0, 10.0, 20.0, 18.0, 5.0, 12)""",
                (tid, now - 500),
            )
        resp = c.get(f"/api/connection-monitor/export/{tid}?resolution=1min")
        assert resp.status_code == 200
        assert "text/csv" in resp.content_type
        content = resp.data.decode()
        reader = csv.reader(io.StringIO(content))
        rows = list(reader)
        assert len(rows) == 2  # header + 1 data row
        assert "avg_latency_ms" in rows[0]
        assert "packet_loss_pct" in rows[0]

    def test_csv_export_auto_blends_every_served_tier(self, client):
        """A window spanning several tiers must export all of them, not an empty file."""
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        storage.save_samples([
            {"target_id": tid, "timestamp": now - 3600, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])
        _save_buckets(storage, tid, 60, [{
            "bucket_start": now - 8 * 86400, "avg_latency_ms": 15.0,
            "min_latency_ms": 10.0, "max_latency_ms": 20.0, "p95_latency_ms": 18.0,
            "packet_loss_pct": 0.0, "sample_count": 12,
        }])
        resp = c.get(
            f"/api/connection-monitor/export/{tid}"
            f"?start={now - 9 * 86400}&end={now}&resolution=auto"
        )
        assert resp.status_code == 200
        rows = list(csv.reader(io.StringIO(resp.data.decode())))
        assert rows[0] == ["datetime", "bucket_seconds", "avg_latency_ms", "min_latency_ms",
                           "max_latency_ms", "p95_latency_ms", "packet_loss_pct", "sample_count"]
        assert len(rows) == 3  # header + aggregated row + raw row
        assert rows[1][1] == "60"
        assert rows[1][2] == "15.0"
        assert rows[2][1] == ""  # raw rows carry no bucket width
        assert rows[2][2] == "10.0"

    def test_csv_export_auto_without_range_stays_raw(self, client):
        """Without an explicit window there is nothing to blend - keep the raw export."""
        c, storage = client
        tid = storage.create_target("Test", "1.1.1.1")
        now = time.time()
        storage.save_samples([
            {"target_id": tid, "timestamp": now, "latency_ms": 10.0, "timeout": False, "probe_method": "tcp"},
        ])
        resp = c.get(f"/api/connection-monitor/export/{tid}?resolution=auto")
        assert resp.status_code == 200
        rows = list(csv.reader(io.StringIO(resp.data.decode())))
        assert rows[0] == ["datetime", "latency_ms", "timeout", "probe_method"]
        assert len(rows) == 2

    def test_pinglog_export_formats_raw_samples_for_isp_evidence(self, client):
        c, storage = client
        tid = storage.create_target("Cloudflare", "1.1.1.1")
        base_ts = datetime(2026, 6, 5, 15, 3, 37, 457000, tzinfo=timezone.utc).timestamp()
        storage.save_samples([
            {"target_id": tid, "timestamp": base_ts, "latency_ms": 28.8, "timeout": False, "probe_method": "icmp"},
            {"target_id": tid, "timestamp": base_ts + 0.989, "latency_ms": None, "timeout": True, "probe_method": "icmp"},
        ])

        resp = c.get(
            f"/api/connection-monitor/export/{tid}"
            f"?start={base_ts - 1}&end={base_ts + 2}&format=pinglog"
        )

        assert resp.status_code == 200
        assert "text/plain" in resp.content_type
        assert "connection_monitor_Cloudflare_raw_ping.log" in resp.headers["Content-Disposition"]
        lines = resp.data.decode().splitlines()
        assert lines[:4] == [
            "# DOCSight raw ping log",
            "# Target: Cloudflare (1.1.1.1)",
            "# Timezone: UTC",
            "# Format: YYYY-MM-DD HH:MM:SS.mmm TZ target status latency unit method",
        ]
        assert "# Range: 2026-06-05 15:03:36.457 UTC to 2026-06-05 15:03:39.457 UTC" in lines
        assert [line for line in lines if line and not line.startswith("#")] == [
            "2026-06-05 15:03:37.457 UTC 1.1.1.1 pong 28.800 ms icmp",
            "2026-06-05 15:03:38.446 UTC 1.1.1.1 timeout - - icmp",
        ]

    def test_pinglog_export_keeps_legacy_csv_filename_semantics(self, client):
        c, storage = client
        tid = storage.create_target("Mein Zähler", "192.0.2.1")

        csv_resp = c.get(f"/api/connection-monitor/export/{tid}")
        pinglog_resp = c.get(f"/api/connection-monitor/export/{tid}?format=pinglog")

        assert "connection_monitor_Mein_Zähler.csv" in csv_resp.headers["Content-Disposition"]
        assert "connection_monitor_Mein_Z_hler_raw_ping.log" in pinglog_resp.headers["Content-Disposition"]

    @pytest.mark.parametrize("label,csv_name,pinglog_name", [
        ('Ziel "A"', "Ziel__A_", "Ziel_A"),
        ("Ziel;A", "Ziel_A", "Ziel_A"),
        ("Ziel\\A", "Ziel_A", "Ziel_A"),
        ("Ziel\r\nX-Injected: 1", "Ziel__X-Injected:_1", "Ziel_X-Injected_1"),
        ("Pfeil → B", "Pfeil___B", "Pfeil_B"),
        ("Ziel \U0001F600", "Ziel__", "Ziel"),
    ])
    def test_export_filenames_stay_header_safe(self, client, label, csv_name, pinglog_name):
        c, storage = client
        tid = storage.create_target(label, "192.0.2.1")

        csv_resp = c.get(f"/api/connection-monitor/export/{tid}")
        pinglog_resp = c.get(f"/api/connection-monitor/export/{tid}?format=pinglog")

        assert csv_resp.status_code == 200
        assert pinglog_resp.status_code == 200
        assert csv_resp.headers["Content-Disposition"] == \
            f"attachment; filename=connection_monitor_{csv_name}.csv"
        assert pinglog_resp.headers["Content-Disposition"] == \
            f"attachment; filename=connection_monitor_{pinglog_name}_raw_ping.log"
        for resp in (csv_resp, pinglog_resp):
            filename = resp.headers["Content-Disposition"].split("filename=", 1)[1]
            assert not set(filename) & set('";\\\r\n')
            filename.encode("latin-1")

    def test_export_rejects_unknown_format(self, client):
        c, storage = client
        tid = storage.create_target("Cloudflare", "1.1.1.1")

        resp = c.get(f"/api/connection-monitor/export/{tid}?format=pinglg")

        assert resp.status_code == 400
        assert resp.get_json() == {"error": "unsupported_format"}


class TestCapabilityAPI:
    def test_capability(self, client):
        c, _ = client
        resp = c.get("/api/connection-monitor/capability")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["method"] == "tcp"


class TestAuthProtection:
    """Verify all endpoints return 401 when auth is enabled but not provided."""

    @pytest.fixture
    def auth_client(self, app):
        """Client with auth enforcement enabled via a mock config manager."""
        flask_app, storage = app
        mock_cfg = MagicMock()
        mock_cfg.get.side_effect = lambda key, default=None: {
            "admin_password": "hashed_pw",
        }.get(key, default)
        mock_cfg.data_dir = os.path.dirname(storage.db_path)
        get_runtime(flask_app).config_manager = mock_cfg
        with flask_app.app_context():
            from app.web import _init_auth_state
            _init_auth_state()
            yield flask_app.test_client(), storage

    def test_targets_get_requires_auth(self, auth_client):
        c, _ = auth_client
        assert c.get("/api/connection-monitor/targets").status_code == 401

    def test_targets_post_requires_auth(self, auth_client):
        c, _ = auth_client
        resp = c.post("/api/connection-monitor/targets", json={"label": "X", "host": "1.1.1.1"})
        assert resp.status_code == 401

    def test_samples_requires_auth(self, auth_client):
        c, _ = auth_client
        assert c.get("/api/connection-monitor/samples/1").status_code == 401

    def test_summary_requires_auth(self, auth_client):
        c, _ = auth_client
        assert c.get("/api/connection-monitor/summary").status_code == 401

    def test_outages_requires_auth(self, auth_client):
        c, _ = auth_client
        assert c.get("/api/connection-monitor/outages/1").status_code == 401

    def test_export_requires_auth(self, auth_client):
        c, _ = auth_client
        assert c.get("/api/connection-monitor/export/1").status_code == 401

    def test_capability_requires_auth(self, auth_client):
        c, _ = auth_client
        assert c.get("/api/connection-monitor/capability").status_code == 401

    def test_authenticated_capability_hides_probe_detection_details(
        self, auth_client
    ):
        c, _ = auth_client
        _auth_session(c, "hashed_pw")
        helper_marker = "ROUTE_HELPER_STDERR_INTERNAL_MARKER"
        raw_marker = "ROUTE_RAW_SOCKET_INTERNAL_MARKER"
        helper_failure = subprocess.CompletedProcess(
            args=["/private/docsight-icmp-helper", "--check"],
            returncode=2,
            stdout="",
            stderr=f"{helper_marker}: /private/helper/config\n",
        )

        with patch(
            "app.modules.connection_monitor.probe.os.path.isfile", return_value=True
        ), patch(
            "app.modules.connection_monitor.probe.os.access", return_value=True
        ), patch(
            "app.modules.connection_monitor.probe.subprocess.run",
            return_value=helper_failure,
        ), patch(
            "app.modules.connection_monitor.probe.socket.socket",
            side_effect=PermissionError(
                f"{raw_marker}: permission denied for raw protocol"
            ),
        ), patch(
            "app.modules.connection_monitor.routes._get_probe_engine",
            side_effect=lambda: ProbeEngine(method="auto"),
        ):
            resp = c.get("/api/connection-monitor/capability")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["method"] == "tcp"
        assert data["reason"] == "ICMP probing is unavailable"
        assert data["hint"] == (
            "Add cap_add: [NET_RAW] to your Docker Compose file "
            "for ICMP probing (more accurate)."
        )
        response_text = resp.get_data(as_text=True)
        assert helper_marker not in response_text
        assert raw_marker not in response_text
        assert "/private/" not in response_text

    def test_pinned_days_get_requires_auth(self, auth_client):
        c, _ = auth_client
        assert c.get("/api/connection-monitor/pinned-days").status_code == 401

    def test_pinned_days_post_requires_auth(self, auth_client):
        c, _ = auth_client
        resp = c.post("/api/connection-monitor/pinned-days", json={"date": "2026-03-10"})
        assert resp.status_code == 401

    def test_pinned_days_delete_requires_auth(self, auth_client):
        c, _ = auth_client
        assert c.delete("/api/connection-monitor/pinned-days/2026-03-10").status_code == 401

    def test_authenticated_request_passes(self, auth_client):
        c, _ = auth_client
        _auth_session(c, "hashed_pw")
        assert c.get("/api/connection-monitor/targets").status_code == 200
