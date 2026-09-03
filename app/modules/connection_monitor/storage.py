"""SQLite storage for Connection Monitor targets and samples."""

import logging
import math
import os

from app.storage.migrations import run_migrations
from app.storage.sqlite import bulk_write, open_read, write_transaction
from .migrations import MIGRATIONS
import time

logger = logging.getLogger(__name__)


def _exclusive_upper_bound(value: float) -> float:
    """Turn an inclusive tier boundary into an exclusive upper limit."""
    return math.nextafter(value, float("-inf"))


class ConnectionMonitorStorage:
    """Manages connection_targets and connection_samples tables."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_tables()

    def _connect(self):
        """Compatibility context for internal test setup writes."""
        return write_transaction(self.db_path)

    def _read(self):
        return open_read(self.db_path)

    def _write(self):
        return write_transaction(self.db_path)

    def _init_tables(self):
        run_migrations(self.db_path, MIGRATIONS)

    # --- Pinned Days ---

    def pin_day(self, date: str, label: str | None = None):
        with self._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO connection_monitor_pinned_days (date, label, created_at) VALUES (?, ?, ?)",
                (date, label, time.time()),
            )

    def unpin_day(self, date: str) -> bool:
        with self._write() as conn:
            cur = conn.execute(
                "DELETE FROM connection_monitor_pinned_days WHERE date = ?", (date,)
            )
            return cur.rowcount > 0

    def get_pinned_days(self) -> list[dict]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM connection_monitor_pinned_days ORDER BY date DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def is_day_pinned(self, date: str) -> bool:
        with self._read() as conn:
            row = conn.execute(
                "SELECT 1 FROM connection_monitor_pinned_days WHERE date = ?", (date,)
            ).fetchone()
            return row is not None

    # --- Target CRUD ---

    def create_target(
        self,
        label: str,
        host: str,
        enabled: bool = True,
        poll_interval_ms: int = 5000,
        probe_method: str = "auto",
        tcp_port: int = 443,
        is_demo: bool = False,
    ) -> int:
        with self._write() as conn:
            cur = conn.execute(
                """INSERT INTO connection_targets
                   (label, host, enabled, poll_interval_ms, probe_method, tcp_port,
                    is_demo, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    label, host, enabled, poll_interval_ms, probe_method,
                    tcp_port, int(is_demo), time.time(),
                ),
            )
            return cur.lastrowid

    def get_targets(self) -> list[dict]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM connection_targets ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_target(self, target_id: int) -> dict | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM connection_targets WHERE id = ?", (target_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_target(self, target_id: int, **fields) -> bool:
        allowed = {"label", "host", "enabled", "poll_interval_ms", "probe_method", "tcp_port"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [target_id]
        with self._write() as conn:
            conn.execute(
                f"UPDATE connection_targets SET {set_clause} WHERE id = ?",
                values,
            )
            return True

    def set_poll_interval_all(self, poll_interval_ms: int):
        """Apply the configured probe interval to every target."""
        with self._write() as conn:
            conn.execute(
                "UPDATE connection_targets SET poll_interval_ms = ?",
                (poll_interval_ms,),
            )

    def delete_target(self, target_id: int):
        with self._write() as conn:
            conn.execute(
                "DELETE FROM connection_targets WHERE id = ?", (target_id,)
            )

    def purge_demo_targets(self) -> int:
        """Delete demo targets and their cascaded samples without touching user targets."""
        with self._write() as conn:
            cur = conn.execute(
                "DELETE FROM connection_targets WHERE is_demo = 1"
            )
            return cur.rowcount

    # --- Samples ---

    def save_samples(self, samples: list[dict]):
        if not samples:
            return
        bulk_write(
            self.db_path,
            """INSERT INTO connection_samples
               (target_id, timestamp, latency_ms, timeout, probe_method)
               VALUES (:target_id, :timestamp, :latency_ms, :timeout, :probe_method)""",
            samples,
        )

    def _build_sample_where(
        self,
        target_id: int,
        start: float | None = None,
        end: float | None = None,
    ) -> tuple[str, list]:
        clauses = ["target_id = ?"]
        params: list[object] = [target_id]
        if start is not None:
            clauses.append("timestamp >= ?")
            params.append(start)
        if end is not None:
            clauses.append("timestamp <= ?")
            params.append(end)
        return " AND ".join(clauses), params

    def get_samples(
        self,
        target_id: int,
        start: float | None = None,
        end: float | None = None,
        limit: int = 10000,
    ) -> list[dict]:
        """Get samples for a target. limit <= 0 means no limit."""
        where, params = self._build_sample_where(target_id, start=start, end=end)

        query = f"SELECT * FROM connection_samples WHERE {where} ORDER BY timestamp"
        if limit > 0:
            query += " LIMIT ?"
            params.append(limit)
        with self._read() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]


    def _aggregated_sub_windows(
        self,
        start: float | None,
        end: float | None,
        now: float,
    ) -> list[tuple[str, int, float | None, float]]:
        """Split the older part of a window into per-tier sub-windows.

        Mirrors how the samples API blends by data age: raw rows cover the
        part newer than _TIER_RAW_MAX_AGE, the 60s/300s/3600s buckets cover
        progressively older parts. Upper bounds are made exclusive so a
        sample can never be counted in two tiers.
        """
        range_start = start if start is not None else float("-inf")
        range_end = end if end is not None else now
        windows: list[tuple[str, int, float | None, float]] = []
        if range_start > range_end:
            return windows
        for name, bucket_seconds, max_age, newer_age in (
            ("1min", 60, self._TIER_60S_MAX_AGE, self._TIER_RAW_MAX_AGE),
            ("5min", 300, self._TIER_300S_MAX_AGE, self._TIER_60S_MAX_AGE),
            ("1hr", 3600, None, self._TIER_300S_MAX_AGE),
        ):
            tier_start = range_start if max_age is None else max(range_start, now - max_age)
            tier_end = _exclusive_upper_bound(min(range_end, now - newer_age))
            if tier_start <= tier_end:
                windows.append((
                    name, bucket_seconds,
                    None if tier_start == float("-inf") else tier_start,
                    tier_end,
                ))
        return windows

    # Raw wins wherever raw exists: a bucket is skipped as soon as any raw row
    # still covers its span, whether because the day is pinned or because
    # aggregate() has not folded it away yet. Backed by idx_samples_target_ts.
    # Cost: at most one bucket per raw/bucket junction is dropped while its
    # span is shared, until the next aggregate() rebuilds it complete.
    _RAW_SURVIVES_EXCLUSION = (
        "NOT EXISTS (SELECT 1 FROM connection_samples cs "
        "WHERE cs.target_id = connection_samples_aggregated.target_id "
        "AND cs.timestamp >= connection_samples_aggregated.bucket_start "
        "AND cs.timestamp < connection_samples_aggregated.bucket_start "
        "+ connection_samples_aggregated.bucket_seconds)"
    )

    def _tier_buckets(
        self,
        target_id: int,
        bucket_seconds: int,
        start: float | None,
        end: float | None,
    ) -> list[dict]:
        """Aggregated buckets for one tier, minus spans whose raw rows survived.

        The exclusion is per bucket and data-driven rather than keyed on the
        pinned-day list, so it stays correct while a just-unpinned day waits
        for the next aggregate(), for a date pinned after its raw was already
        pruned, and for buckets straddling local midnight.
        """
        clauses = ["target_id = ?", "bucket_seconds = ?", self._RAW_SURVIVES_EXCLUSION]
        params: list[object] = [target_id, bucket_seconds]
        if start is not None:
            clauses.append("bucket_start >= ?")
            params.append(start)
        if end is not None:
            clauses.append("bucket_start <= ?")
            params.append(end)
        where = " AND ".join(clauses)
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT * FROM connection_samples_aggregated WHERE {where} ORDER BY bucket_start",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def get_samples_bucketed(
        self,
        target_id: int,
        start: float,
        end: float,
        bucket_seconds: int,
        anchor: float,
    ) -> list[dict]:
        """Aggregate raw samples into fixed buckets indexed from anchor.

        Lets the chart API pre-compress the raw tier in SQLite instead of
        reading every row. Rows are shaped like get_aggregated_samples(); the
        latency stats cover every sample that carries a latency (timeouts only
        add to the sample count and to packet loss) and p95 is nearest-rank.
        """
        params = {
            "target_id": target_id,
            "start": start,
            "end": end,
            "width": bucket_seconds,
            "anchor": anchor,
        }
        with self._read() as conn:
            rows = conn.execute(
                """
                SELECT bucket_idx, sample_count, timeout_count, avg_latency_ms,
                       min_latency_ms, max_latency_ms, latency_ms AS p95_latency_ms
                FROM (
                    SELECT bucket_idx, latency_ms,
                           ROW_NUMBER() OVER ordered AS rn,
                           COUNT(*) OVER whole AS sample_count,
                           SUM(timeout) OVER whole AS timeout_count,
                           COUNT(latency_ms) OVER whole AS latency_count,
                           AVG(latency_ms) OVER whole AS avg_latency_ms,
                           MIN(latency_ms) OVER whole AS min_latency_ms,
                           MAX(latency_ms) OVER whole AS max_latency_ms
                    FROM (
                        SELECT CAST((timestamp - :anchor) / :width AS INTEGER) AS bucket_idx,
                               latency_ms, timeout
                        FROM connection_samples
                        WHERE target_id = :target_id
                          AND timestamp >= :start AND timestamp <= :end
                    )
                    WINDOW whole AS (PARTITION BY bucket_idx),
                           ordered AS (PARTITION BY bucket_idx
                                       ORDER BY latency_ms IS NULL, latency_ms)
                )
                -- one row per bucket: the nearest-rank p95 latency, which also
                -- carries that bucket's totals. Buckets without a latency keep
                -- their first row: p95 comes out NULL (the row's latency_ms is
                -- NULL), the count/timeout totals stay correct.
                WHERE rn = CAST(latency_count * 0.95 AS INTEGER) + 1
                ORDER BY bucket_idx
                """,
                params,
            ).fetchall()
            return [
                {
                    "bucket_start": anchor + r["bucket_idx"] * bucket_seconds,
                    "bucket_seconds": bucket_seconds,
                    "avg_latency_ms": r["avg_latency_ms"],
                    "min_latency_ms": r["min_latency_ms"],
                    "max_latency_ms": r["max_latency_ms"],
                    "p95_latency_ms": r["p95_latency_ms"],
                    "packet_loss_pct": round(
                        100.0 * r["timeout_count"] / r["sample_count"], 2
                    ),
                    "sample_count": r["sample_count"],
                }
                for r in rows
            ]


    def get_range_stats(
        self,
        target_id: int,
        start: float | None = None,
        end: float | None = None,
    ) -> dict[str, object]:
        """Range statistics blended across raw samples and aggregated tiers.

        Raw rows normally survive only _TIER_RAW_MAX_AGE, so a window reaching
        further back is completed from the 60s/300s/3600s buckets. The split is
        data-driven rather than age-driven: raw is read over the whole window
        and a bucket is skipped whenever raw still covers its span, so raw wins
        wherever raw exists. That keeps pinned days exact, covers raw rows that
        aggregate() has not folded away yet (the demo collector, a stopped
        collector, or simply the up-to-15-minute lag at the 7d edge), and still
        cannot double count.
        ``tiers_used`` lists the tiers that actually contributed rows.

        p95 is approximate as soon as buckets contribute, and biased HIGH:
        each bucket's own p95 stands in for all of that bucket's samples,
        weighted by its estimated latency count. Taking MAX() of the bucket p95
        values instead would degenerate into "the worst minute of the window"
        over long ranges.
        """
        now = time.time()
        agg_windows = self._aggregated_sub_windows(start, end, now)
        if agg_windows:
            return self._blended_range_stats(target_id, start, end, now, agg_windows)

        where, params = self._build_sample_where(target_id, start=start, end=end)
        with self._read() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS sample_count,
                    COUNT(CASE WHEN timeout = 0 AND latency_ms IS NOT NULL THEN 1 END) AS latency_count,
                    AVG(CASE WHEN timeout = 0 THEN latency_ms END) AS avg_latency_ms,
                    MIN(CASE WHEN timeout = 0 THEN latency_ms END) AS min_latency_ms,
                    MAX(CASE WHEN timeout = 0 THEN latency_ms END) AS max_latency_ms,
                    ROUND(
                        100.0 * SUM(CASE WHEN timeout = 1 THEN 1 ELSE 0 END) / MAX(COUNT(*), 1),
                        2
                    ) AS packet_loss_pct
                FROM connection_samples
                WHERE {where}
                """,
                params,
            ).fetchone()
            stats = dict(row) if row else {}
            latency_count = stats.get("latency_count") or 0
            if latency_count > 0:
                # Same nearest-rank sample, counted from the slowest end: the
                # bounded sorter then only has to keep the top 5% of the window
                # instead of ordering all of it.
                p95_offset = latency_count - self._p95_rank(latency_count)
                p95_row = conn.execute(
                    f"""
                    SELECT latency_ms
                    FROM connection_samples
                    WHERE {where} AND timeout = 0 AND latency_ms IS NOT NULL
                    ORDER BY latency_ms DESC
                    LIMIT 1 OFFSET ?
                    """,
                    [*params, p95_offset],
                ).fetchone()
                stats["p95_latency_ms"] = p95_row["latency_ms"] if p95_row else None
            else:
                stats["p95_latency_ms"] = None
            stats["tiers_used"] = ["raw"] if stats.get("sample_count") else []
            return stats

    def _blended_range_stats(
        self,
        target_id: int,
        start: float | None,
        end: float | None,
        now: float,
        agg_windows: list[tuple[str, int, float | None, float]],
    ) -> dict[str, object]:
        """Combine raw rows with aggregated buckets for get_range_stats."""
        tiers_used: list[str] = []
        sample_count = 0
        latency_count = 0
        latency_sum = 0.0
        timeout_total = 0.0
        min_latency = None
        max_latency = None
        # Bucket p95 stand-ins as (value, weight); the raw side stays in
        # SQLite until the weights are known, then only its slowest values are
        # read - a 30d window never materialises 100k+ latencies.
        bucket_p95: list[tuple[float, int]] = []

        where, params = self._build_sample_where(target_id, start=start, end=end)
        with self._read() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS sample_count,
                    SUM(CASE WHEN timeout = 1 THEN 1 ELSE 0 END) AS timeout_count,
                    COUNT(CASE WHEN timeout = 0 AND latency_ms IS NOT NULL THEN 1 END) AS latency_count,
                    SUM(CASE WHEN timeout = 0 THEN latency_ms END) AS latency_sum,
                    MIN(CASE WHEN timeout = 0 THEN latency_ms END) AS min_latency_ms,
                    MAX(CASE WHEN timeout = 0 THEN latency_ms END) AS max_latency_ms
                FROM connection_samples
                WHERE {where}
                """,
                params,
            ).fetchone()
        if row["sample_count"]:
            tiers_used.append("raw")
            sample_count += row["sample_count"]
            timeout_total += row["timeout_count"] or 0
        if row["latency_count"]:
            latency_count += row["latency_count"]
            latency_sum += row["latency_sum"] or 0.0
            min_latency = row["min_latency_ms"]
            max_latency = row["max_latency_ms"]

        for name, bucket_seconds, tier_start, tier_end in agg_windows:
            buckets = self._tier_buckets(target_id, bucket_seconds, tier_start, tier_end)
            if not buckets:
                continue
            tiers_used.append(name)
            for b in buckets:
                count = b["sample_count"] or 0
                loss_pct = b["packet_loss_pct"] or 0.0
                sample_count += count
                timeout_total += count * loss_pct / 100.0
                # Buckets keep no per-sample latencies, so estimate how many
                # of them carried one from the recorded packet loss.
                latency_est = round(count * (1.0 - loss_pct / 100.0))
                if latency_est <= 0:
                    continue
                if b["avg_latency_ms"] is not None:
                    latency_count += latency_est
                    latency_sum += b["avg_latency_ms"] * latency_est
                if b["min_latency_ms"] is not None:
                    min_latency = (
                        b["min_latency_ms"] if min_latency is None
                        else min(min_latency, b["min_latency_ms"])
                    )
                if b["max_latency_ms"] is not None:
                    max_latency = (
                        b["max_latency_ms"] if max_latency is None
                        else max(max_latency, b["max_latency_ms"])
                    )
                if b["p95_latency_ms"] is not None:
                    bucket_p95.append((b["p95_latency_ms"], latency_est))

        # The quantile walk starts at the slowest value and stops at the rank,
        # so the raw side only has to supply the values above it: at most the
        # top 5% of the window plus one.
        raw_latencies = row["latency_count"] or 0
        total_weight = raw_latencies + sum(w for _, w in bucket_p95)
        tail = self._slowest_raw_latencies(
            where, params,
            min(raw_latencies, total_weight - self._p95_rank(total_weight) + 1),
        )
        p95_latency = self._weighted_p95(tail, raw_latencies, bucket_p95)

        return {
            "sample_count": sample_count,
            "latency_count": latency_count,
            "avg_latency_ms": latency_sum / latency_count if latency_count > 0 else None,
            "min_latency_ms": min_latency,
            "max_latency_ms": max_latency,
            "packet_loss_pct": (
                round(timeout_total / sample_count * 100, 2) if sample_count else None
            ),
            "p95_latency_ms": p95_latency,
            "tiers_used": tiers_used,
        }

    @staticmethod
    def _p95_rank(total_weight: int) -> int:
        """1-based nearest-rank position of the p95 in a weighted window."""
        return max(1, math.ceil(total_weight * 0.95))

    def _slowest_raw_latencies(
        self,
        where: str,
        params: list,
        limit: int,
    ) -> list[float]:
        """The slowest raw latencies of a window, slowest first.

        A bounded ORDER BY costs SQLite a top-N sorter instead of ordering
        every latency in the window.
        """
        if limit <= 0:
            return []
        with self._read() as conn:
            return [
                r[0] for r in conn.execute(
                    f"""
                    SELECT latency_ms
                    FROM connection_samples
                    WHERE {where} AND timeout = 0 AND latency_ms IS NOT NULL
                    ORDER BY latency_ms DESC
                    LIMIT ?
                    """,
                    [*params, limit],
                )
            ]

    @classmethod
    def _weighted_p95(
        cls,
        raw_tail: list[float],
        raw_count: int,
        bucket_p95: list[tuple[float, int]],
    ) -> float | None:
        """Weighted nearest-rank p95 over raw values plus bucket stand-ins.

        Both inputs are walked two-pointer style from the slowest value down,
        counting the remaining weight towards the rank. Walking down rather
        than up keeps the raw side to ``raw_tail``, the slowest values of the
        window; ``raw_count`` is how many raw latencies it stands for.
        """
        raw_len = len(raw_tail)
        bucket_len = len(bucket_p95)
        total_weight = raw_count + sum(w for _, w in bucket_p95)
        if total_weight <= 0:
            return None
        bucket_p95.sort(key=lambda pair: pair[0], reverse=True)
        rank = cls._p95_rank(total_weight)
        remaining = total_weight
        value = None
        i = j = 0
        while i < raw_len or j < bucket_len:
            if j >= bucket_len or (i < raw_len and raw_tail[i] >= bucket_p95[j][0]):
                value, weight = raw_tail[i], 1
                i += 1
            else:
                value, weight = bucket_p95[j]
                j += 1
            if remaining - weight < rank:
                break
            remaining -= weight
        return value

    # --- Summary ---

    def get_summary(self, target_id: int, window_seconds: int = 60) -> dict[str, object]:
        cutoff = time.time() - window_seconds
        with self._read() as conn:
            row = conn.execute(
                """SELECT
                    COUNT(*) as sample_count,
                    AVG(CASE WHEN timeout = 0 THEN latency_ms END) as avg_latency_ms,
                    MIN(CASE WHEN timeout = 0 THEN latency_ms END) as min_latency_ms,
                    MAX(CASE WHEN timeout = 0 THEN latency_ms END) as max_latency_ms,
                    ROUND(100.0 * SUM(CASE WHEN timeout = 1 THEN 1 ELSE 0 END) / MAX(COUNT(*), 1), 2) as packet_loss_pct
                FROM connection_samples
                WHERE target_id = ? AND timestamp >= ?""",
                (target_id, cutoff),
            ).fetchone()
            return dict(row) if row else {}

    # --- Outages ---

    def get_outages(
        self,
        target_id: int,
        threshold: int = 5,
        start: float | None = None,
        end: float | None = None,
    ) -> list[dict]:
        """Derive outages from consecutive timeout sequences.

        Raw rows that aggregate() has already folded away are replaced by runs
        of fully-lost aggregated buckets, flagged with "approximate": True. As
        in get_range_stats the split is data-driven: raw is read over the whole
        window and a bucket is skipped whenever raw still covers its span, so
        raw wins wherever raw exists. Surviving raw is therefore not
        contiguous, and a run is cut wherever raw coverage has a gap. A bucket
        run adjacent to a raw run is merged with it once, before the threshold
        is applied, so an outage straddling the boundary is not lost to both
        halves being too short. Bucket runs are never merged with each other
        because the bucket sizes differ across a tier boundary.
        """
        now = time.time()
        agg_windows = self._aggregated_sub_windows(start, end, now)
        if not agg_windows:
            return self._raw_outages(target_id, threshold, start, end)

        candidates = self._raw_outage_candidates(target_id, start, end)
        newest_bucket_end = None
        for _name, bucket_seconds, tier_start, tier_end in agg_windows:
            buckets = self._tier_buckets(target_id, bucket_seconds, tier_start, tier_end)
            if not buckets:
                continue
            coverage_end = buckets[-1]["bucket_start"] + bucket_seconds
            if newest_bucket_end is None or coverage_end > newest_bucket_end:
                newest_bucket_end = coverage_end
            candidates.extend(self._bucket_outage_candidates(buckets, bucket_seconds))

        candidates.sort(key=lambda c: c["start"])
        # As in the raw-only path an unterminated run is still running, but
        # only when nothing in the window covers a later moment.
        if candidates:
            newest = candidates[-1]
            if newest.pop("open", False) and (
                newest_bucket_end is None or newest_bucket_end <= newest["duration_end"]
            ):
                newest["end"] = None
        return self._merge_outage_candidates(candidates, threshold)

    @staticmethod
    def _bucket_outage_candidates(buckets: list[dict], bucket_seconds: int) -> list[dict]:
        """Unthresholded outage runs of time-adjacent 100%-loss buckets."""
        candidates: list[dict] = []
        run: list[dict] = []

        def close_run():
            if not run:
                return
            run_start = run[0]["bucket_start"]
            run_end = run[-1]["bucket_start"] + bucket_seconds
            candidates.append({
                "start": run_start,
                "end": run_end,
                "duration_end": run_end,
                "timeout_count": sum(b["sample_count"] or 0 for b in run),
                "approximate": True,
                "bucket_seconds": bucket_seconds,
            })
            run.clear()

        for b in buckets:
            lost = b["packet_loss_pct"] == 100.0
            if lost and run and b["bucket_start"] != run[-1]["bucket_start"] + bucket_seconds:
                close_run()
            if not lost:
                close_run()
                continue
            run.append(b)
        close_run()
        return candidates

    # A wider hole than this in the surviving raw rows is missing coverage,
    # not a continuous outage. Slow targets widen it: a handful of missed
    # polls is still one outage, and no setting caps poll_interval_ms.
    _RAW_COVERAGE_GAP = 3600

    def _raw_outage_candidates(
        self,
        target_id: int,
        start: float | None,
        end: float | None,
    ) -> list[dict]:
        """Unthresholded raw timeout runs, cut at gaps in raw coverage."""
        target = self.get_target(target_id)
        poll_interval_ms = (target or {}).get("poll_interval_ms") or 0
        max_gap = max(self._RAW_COVERAGE_GAP, 5 * poll_interval_ms / 1000.0)

        where, params = self._build_sample_where(target_id, start=start, end=end)
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT timestamp, timeout FROM connection_samples WHERE {where} ORDER BY timestamp",
                params,
            ).fetchall()

        segments: list[list] = []
        for row in rows:
            if segments and row["timestamp"] - segments[-1][-1]["timestamp"] <= max_gap:
                segments[-1].append(row)
            else:
                segments.append([row])

        candidates: list[dict] = []
        for index, segment in enumerate(segments):
            # Only the newest segment can hold a still-running outage; whether
            # it really is the newest coverage is settled by the caller.
            candidates.extend(self._segment_outage_candidates(
                segment, index == len(segments) - 1,
            ))
        return candidates

    @staticmethod
    def _segment_outage_candidates(rows: list, allow_ongoing: bool) -> list[dict]:
        candidates: list[dict] = []
        run_start = None
        run_count = 0
        for row in rows:
            if row["timeout"]:
                if run_start is None:
                    run_start = row["timestamp"]
                run_count += 1
                continue
            if run_count:
                candidates.append({
                    "start": run_start,
                    "end": row["timestamp"],
                    "duration_end": row["timestamp"],
                    "timeout_count": run_count,
                    "approximate": False,
                    "bucket_seconds": None,
                })
            run_start = None
            run_count = 0
        if run_count:
            last_ts = rows[-1]["timestamp"]
            candidates.append({
                "start": run_start,
                "end": last_ts,
                "duration_end": last_ts,
                "timeout_count": run_count,
                "approximate": False,
                "bucket_seconds": None,
                # this run has no terminating sample, so it may still be open
                "open": allow_ongoing,
            })
        return candidates

    @staticmethod
    def _outage_candidates_adjacent(prev: dict, cand: dict) -> bool:
        """Whether a bucket run and a raw run belong to the same outage."""
        if prev["approximate"] == cand["approximate"] or prev["end"] is None:
            return False
        # One merge per outage: a merged run must not look like a bucket run
        # to the next raw candidate and swallow it too.
        if prev.get("merged"):
            return False
        slack = prev["bucket_seconds"] or cand["bucket_seconds"]
        # A bucket run is clamped to the raw run's first sample so the two
        # halves of a merged outage cannot overlap.
        prev_end = min(prev["end"], cand["start"]) if prev["bucket_seconds"] else prev["end"]
        return cand["start"] <= prev_end + slack

    def _merge_outage_candidates(self, candidates: list[dict], threshold: int) -> list[dict]:
        """Join boundary-straddling runs, then apply the threshold once."""
        merged: list[dict] = []
        for cand in candidates:
            if merged and self._outage_candidates_adjacent(merged[-1], cand):
                prev = merged[-1]
                prev["end"] = cand["end"]
                prev["duration_end"] = cand["duration_end"]
                prev["timeout_count"] += cand["timeout_count"]
                prev["approximate"] = True
                prev["bucket_seconds"] = prev["bucket_seconds"] or cand["bucket_seconds"]
                prev["merged"] = True
                continue
            merged.append(dict(cand))

        outages = []
        for cand in merged:
            if cand["timeout_count"] < threshold:
                continue
            outage = {
                "start": cand["start"],
                "end": cand["end"],
                "duration_seconds": round(cand["duration_end"] - cand["start"], 1),
                "timeout_count": cand["timeout_count"],
            }
            if cand["approximate"]:
                outage["approximate"] = True
            outages.append(outage)
        return outages

    def _raw_outages(
        self,
        target_id: int,
        threshold: int,
        start: float | None,
        end: float | None,
    ) -> list[dict]:
        """Derive outages from consecutive timeout sequences in raw samples."""
        clauses = ["target_id = ?"]
        params: list[object] = [target_id]
        if start is not None:
            clauses.append("timestamp >= ?")
            params.append(start)
        if end is not None:
            clauses.append("timestamp <= ?")
            params.append(end)
        where = " AND ".join(clauses)
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT timestamp, timeout FROM connection_samples WHERE {where} ORDER BY timestamp",
                params,
            ).fetchall()

        outages = []
        run_start = None
        run_count = 0
        for row in rows:
            if row["timeout"]:
                if run_start is None:
                    run_start = row["timestamp"]
                run_count += 1
            else:
                if run_count >= threshold:
                    outages.append({
                        "start": run_start,
                        "end": row["timestamp"],
                        "duration_seconds": round(row["timestamp"] - run_start, 1),
                        "timeout_count": run_count,
                    })
                run_start = None
                run_count = 0
        # Handle ongoing outage at end of data
        if run_count >= threshold:
            last_ts = rows[-1]["timestamp"] if rows else time.time()
            outages.append({
                "start": run_start,
                "end": None,
                "duration_seconds": round(last_ts - run_start, 1),
                "timeout_count": run_count,
            })
        return outages

    # --- Aggregation ---

    def get_aggregated_samples(
        self,
        target_id: int,
        bucket_seconds: int,
        start: float | None = None,
        end: float | None = None,
    ) -> list[dict]:
        """Get aggregated samples for a target at a specific resolution."""
        clauses = ["target_id = ?", "bucket_seconds = ?"]
        params: list[object] = [target_id, bucket_seconds]
        if start is not None:
            clauses.append("bucket_start >= ?")
            params.append(start)
        if end is not None:
            clauses.append("bucket_start <= ?")
            params.append(end)
        where = " AND ".join(clauses)
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT * FROM connection_samples_aggregated WHERE {where} ORDER BY bucket_start",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def aggregate_raw_to_buckets(
        self, target_id: int, cutoff: float, bucket_seconds: int = 60
    ) -> int:
        """Aggregate raw samples older than cutoff into fixed-size buckets.

        Computes avg/min/max/p95 latency, packet loss %, and sample count
        per bucket. Deletes aggregated raw samples. Returns number of
        buckets created.
        """
        with self._write() as conn:
            rows = conn.execute(
                """SELECT timestamp, latency_ms, timeout
                   FROM connection_samples
                   WHERE target_id = ? AND timestamp < ?
                   ORDER BY timestamp""",
                (target_id, cutoff),
            ).fetchall()

            if not rows:
                return 0

            buckets: dict[float, list] = {}
            for row in rows:
                bucket_start = (row["timestamp"] // bucket_seconds) * bucket_seconds
                if bucket_start not in buckets:
                    buckets[bucket_start] = []
                buckets[bucket_start].append(row)

            created = 0
            for bucket_start, samples in buckets.items():
                latencies = [
                    s["latency_ms"] for s in samples
                    if not s["timeout"] and s["latency_ms"] is not None
                ]
                total = len(samples)
                timeouts = sum(1 for s in samples if s["timeout"])

                avg_lat = sum(latencies) / len(latencies) if latencies else None
                min_lat = min(latencies) if latencies else None
                max_lat = max(latencies) if latencies else None
                loss_pct = round(timeouts / total * 100, 2) if total > 0 else 0.0

                # p95 via nearest-rank
                p95_lat = None
                if latencies:
                    sorted_lat = sorted(latencies)
                    idx = min(int(len(sorted_lat) * 0.95), len(sorted_lat) - 1)
                    p95_lat = sorted_lat[idx]

                conn.execute(
                    """INSERT OR REPLACE INTO connection_samples_aggregated
                       (target_id, bucket_start, bucket_seconds,
                        avg_latency_ms, min_latency_ms, max_latency_ms,
                        p95_latency_ms, packet_loss_pct, sample_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (target_id, bucket_start, bucket_seconds,
                     avg_lat, min_lat, max_lat, p95_lat, loss_pct, total),
                )
                created += 1

            conn.execute(
                """DELETE FROM connection_samples WHERE target_id = ? AND timestamp < ?
                   AND date(timestamp, 'unixepoch', 'localtime')
                       NOT IN (SELECT date FROM connection_monitor_pinned_days)""",
                (target_id, cutoff),
            )

            if created:
                logger.info(
                    "Connection Monitor: aggregated %d raw samples into %d buckets (%ds) for target %d",
                    len(rows), created, bucket_seconds, target_id,
                )
            return created

    def reaggregate_buckets(
        self,
        target_id: int,
        cutoff: float,
        source_seconds: int,
        target_seconds: int,
    ) -> int:
        """Roll up smaller aggregated buckets into larger ones.

        Aggregates source_seconds buckets older than cutoff into
        target_seconds buckets, then deletes the source buckets.
        p95 is approximated as MAX(p95) of constituent buckets.
        Returns number of target buckets created.
        """
        with self._write() as conn:
            rows = conn.execute(
                """SELECT bucket_start, avg_latency_ms, min_latency_ms,
                          max_latency_ms, p95_latency_ms, packet_loss_pct,
                          sample_count
                   FROM connection_samples_aggregated
                   WHERE target_id = ? AND bucket_seconds = ? AND bucket_start < ?
                   ORDER BY bucket_start""",
                (target_id, source_seconds, cutoff),
            ).fetchall()

            if not rows:
                return 0

            # Group into target-sized buckets
            buckets: dict[float, list] = {}
            for row in rows:
                bucket_start = (row["bucket_start"] // target_seconds) * target_seconds
                if bucket_start not in buckets:
                    buckets[bucket_start] = []
                buckets[bucket_start].append(row)

            created = 0
            for bucket_start, sources in buckets.items():
                total_count = sum(s["sample_count"] for s in sources)
                # Weighted average for avg_latency
                non_null = [s for s in sources if s["avg_latency_ms"] is not None]
                if non_null:
                    weight_sum = sum(s["sample_count"] for s in non_null)
                    avg_lat = sum(
                        s["avg_latency_ms"] * s["sample_count"] for s in non_null
                    ) / weight_sum if weight_sum > 0 else None
                    min_lat = min(s["min_latency_ms"] for s in non_null)
                    max_lat = max(s["max_latency_ms"] for s in non_null)
                    p95_vals = [s["p95_latency_ms"] for s in non_null if s["p95_latency_ms"] is not None]
                    p95_lat = max(p95_vals) if p95_vals else None
                else:
                    avg_lat = min_lat = max_lat = p95_lat = None

                # Weighted loss
                loss_pct = round(
                    sum(s["packet_loss_pct"] * s["sample_count"] for s in sources)
                    / total_count, 2
                ) if total_count > 0 else 0.0

                conn.execute(
                    """INSERT OR REPLACE INTO connection_samples_aggregated
                       (target_id, bucket_start, bucket_seconds,
                        avg_latency_ms, min_latency_ms, max_latency_ms,
                        p95_latency_ms, packet_loss_pct, sample_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (target_id, bucket_start, target_seconds,
                     avg_lat, min_lat, max_lat, p95_lat, loss_pct, total_count),
                )
                created += 1

            # Delete source buckets
            conn.execute(
                """DELETE FROM connection_samples_aggregated
                   WHERE target_id = ? AND bucket_seconds = ? AND bucket_start < ?""",
                (target_id, source_seconds, cutoff),
            )

            if created:
                logger.info(
                    "Connection Monitor: re-aggregated %d x %ds buckets into %d x %ds buckets for target %d",
                    len(rows), source_seconds, created, target_seconds, target_id,
                )
            return created

    # Tier boundaries in seconds
    _TIER_RAW_MAX_AGE = 7 * 86400       # 7 days
    _TIER_60S_MAX_AGE = 30 * 86400      # 30 days
    _TIER_300S_MAX_AGE = 90 * 86400     # 90 days

    def aggregate(self):
        """Run the full aggregation cascade for all targets.

        1. Raw samples older than 7d -> 60s buckets
        2. 60s buckets older than 30d -> 300s buckets
        3. 300s buckets older than 90d -> 3600s buckets
        """
        now = time.time()
        targets = self.get_targets()
        for t in targets:
            tid = t["id"]
            # Step 1: raw -> 60s
            self.aggregate_raw_to_buckets(
                tid, cutoff=now - self._TIER_RAW_MAX_AGE, bucket_seconds=60
            )
            # Step 2: 60s -> 300s
            self.reaggregate_buckets(
                tid, cutoff=now - self._TIER_60S_MAX_AGE,
                source_seconds=60, target_seconds=300
            )
            # Step 3: 300s -> 3600s
            self.reaggregate_buckets(
                tid, cutoff=now - self._TIER_300S_MAX_AGE,
                source_seconds=300, target_seconds=3600
            )

    # --- Traceroute CRUD ---

    def save_trace(self, target_id, timestamp, trigger_reason, hops,
                   route_fingerprint, reached_target, is_demo=False):
        with self._write() as conn:
            cur = conn.execute(
                """INSERT INTO traceroute_traces
                   (target_id, timestamp, trigger_reason, hop_count,
                    route_fingerprint, reached_target, is_demo)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (target_id, timestamp, trigger_reason, len(hops),
                 route_fingerprint, int(reached_target), int(is_demo)),
            )
            trace_id = cur.lastrowid
            if hops:
                for hop in hops:
                    conn.execute(
                        """INSERT INTO traceroute_hops
                           (trace_id, hop_index, hop_ip, hop_host, latency_ms, probes_responded)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (trace_id, hop["hop_index"], hop["hop_ip"], hop.get("hop_host"),
                         hop.get("latency_ms"), hop.get("probes_responded", 0)),
                    )
            return trace_id

    def get_traces(self, target_id, start=None, end=None, limit=100):
        with self._read() as conn:
            sql = "SELECT * FROM traceroute_traces WHERE target_id = ?"
            params = [target_id]
            if start is not None:
                sql += " AND timestamp >= ?"
                params.append(start)
            if end is not None:
                sql += " AND timestamp <= ?"
                params.append(end)
            sql += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_trace(self, trace_id):
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM traceroute_traces WHERE id = ?", (trace_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_trace_hops(self, trace_id):
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM traceroute_hops WHERE trace_id = ? ORDER BY hop_index",
                (trace_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def cleanup_traces(self, retention_days):
        if not retention_days:
            return
        cutoff = time.time() - (retention_days * 86400)
        pinned = self.get_pinned_days()
        with self._write() as conn:
            if pinned:
                placeholders = ",".join("?" * len(pinned))
                conn.execute(
                    f"DELETE FROM traceroute_traces WHERE timestamp < ? AND date(timestamp, 'unixepoch') NOT IN ({placeholders})",
                    [cutoff] + [p["date"] for p in pinned],
                )
            else:
                conn.execute("DELETE FROM traceroute_traces WHERE timestamp < ?", (cutoff,))

    def purge_demo_traces(self):
        with self._write() as conn:
            conn.execute("DELETE FROM traceroute_traces WHERE is_demo = 1")

    # --- Retention ---

    def cleanup(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = time.time() - (retention_days * 86400)
        with self._write() as conn:
            cur = conn.execute(
                """DELETE FROM connection_samples WHERE timestamp < ?
                   AND date(timestamp, 'unixepoch', 'localtime')
                       NOT IN (SELECT date FROM connection_monitor_pinned_days)""",
                (cutoff,),
            )
            deleted = cur.rowcount
            cur2 = conn.execute(
                "DELETE FROM connection_samples_aggregated WHERE bucket_start < ?",
                (cutoff,),
            )
            deleted += cur2.rowcount
            if deleted:
                logger.info("Connection Monitor: cleaned up %d old samples/buckets", deleted)
            return deleted
