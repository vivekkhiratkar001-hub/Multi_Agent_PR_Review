import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


MAX_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (5, 15)
MAX_RETRY_AFTER_SECONDS = 300
DEFAULT_LEASE_SECONDS = 300

VALID_STATUSES = {
    "pending",
    "processing",
    "completed",
    "failed",
    "retryable",
}


class JobStore:
    """Persist review jobs, prevent duplicate deliveries, and track recovery."""

    def __init__(
        self,
        db_path: str | Path,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ):
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least 1")

        self.db_path = str(db_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_seconds = lease_seconds

        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT NOT NULL UNIQUE,
                    repository TEXT NOT NULL,
                    pull_request_number INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            # Add columns without deleting existing records.
            existing_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(review_jobs)"
                ).fetchall()
            }

            migrations = {
                "attempt_count": (
                    "INTEGER NOT NULL DEFAULT 0"
                ),
                "last_error": "TEXT",
                "updated_at": "TEXT",
                "retry_eligible_at": "TEXT",
                "claim_token": "TEXT",
                "lease_expires_at": "TEXT",
                "started_at": "TEXT",
                "completed_at": "TEXT",
                "pull_request_url": "TEXT",
                "head_sha": "TEXT",
                "title": "TEXT",
                "changed_files_json": "TEXT",
            }

            for column, definition in migrations.items():
                if column not in existing_columns:
                    connection.execute(
                        f"ALTER TABLE review_jobs "
                        f"ADD COLUMN {column} {definition}"
                    )

            connection.execute(
                """
                UPDATE review_jobs
                SET attempt_count = 1
                WHERE status = 'processing'
                  AND attempt_count = 0
                """
            )

            connection.execute(
                """
                UPDATE review_jobs
                SET updated_at = created_at
                WHERE updated_at IS NULL
                """
            )
            connection.execute(
                """
                UPDATE review_jobs
                SET lease_expires_at = datetime(
                    COALESCE(updated_at, created_at),
                    ?
                )
                WHERE status = 'processing'
                  AND lease_expires_at IS NULL
                """,
                (f"+{self.lease_seconds} seconds",),
            )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _timestamp(self, value: datetime | None = None) -> str:
        return (value or self._now()).strftime("%Y-%m-%d %H:%M:%S")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def record_job(
        self,
        delivery_id: str,
        repository: str,
        pull_request_number: int,
        action: str,
    ) -> bool:
        """Insert a job; return False if the delivery already exists."""
        now = self._timestamp()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO review_jobs
                    (delivery_id, repository, pull_request_number, action,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    repository,
                    pull_request_number,
                    action,
                    now,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def claim_job(self, delivery_id: str) -> bool:
        """Atomically claim a job while preserving the original bool API."""
        return self.claim_job_with_token(delivery_id) is not None

    def claim_job_with_token(self, delivery_id: str) -> str | None:
        """Claim an eligible job or reclaim its expired processing lease."""
        claim_token = uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now_datetime = self._now()
            now = self._timestamp(now_datetime)
            lease_expires_at = self._timestamp(
                now_datetime + timedelta(seconds=self.lease_seconds)
            )
            job = connection.execute(
                """
                SELECT attempt_count, lease_expires_at, retry_eligible_at
                FROM review_jobs
                WHERE delivery_id = ?
                  AND status = 'processing'
                  AND lease_expires_at <= ?
                  AND attempt_count < ?
                """,
                (delivery_id, now, MAX_ATTEMPTS),
            ).fetchone()
            if job is not None and job["retry_eligible_at"] is None:
                # Schedule recovery once from the expired lease, not from each
                # poll; the persisted deadline survives restarts unchanged.
                delay_index = max(
                    0,
                    min(
                        int(job["attempt_count"]) - 1,
                        len(RETRY_DELAYS_SECONDS) - 1,
                    ),
                )
                delay = RETRY_DELAYS_SECONDS[delay_index]
                connection.execute(
                    """
                    UPDATE review_jobs
                    SET retry_eligible_at = datetime(lease_expires_at, ?)
                    WHERE delivery_id = ?
                      AND status = 'processing'
                      AND lease_expires_at <= ?
                      AND retry_eligible_at IS NULL
                    """,
                    (f"+{delay} seconds", delivery_id, now),
                )

            connection.execute(
                """
                UPDATE review_jobs
                SET status = 'failed',
                    last_error = COALESCE(
                        last_error,
                        'Processing lease expired after maximum attempts'
                    ),
                    updated_at = ?,
                    completed_at = ?,
                    claim_token = NULL,
                    lease_expires_at = NULL,
                    retry_eligible_at = NULL
                WHERE delivery_id = ?
                  AND (
                    status IN ('pending', 'retryable')
                    OR (
                        status = 'processing'
                        AND lease_expires_at <= ?
                    )
                  )
                  AND attempt_count >= ?
                """,
                (now, now, delivery_id, now, MAX_ATTEMPTS),
            )
            cursor = connection.execute(
                """
                UPDATE review_jobs
                SET status = 'processing',
                    attempt_count = attempt_count + 1,
                    last_error = NULL,
                    updated_at = ?,
                    started_at = COALESCE(started_at, ?),
                    completed_at = NULL,
                    retry_eligible_at = NULL,
                    claim_token = ?,
                    lease_expires_at = ?
                WHERE delivery_id = ?
                  AND attempt_count < ?
                  AND (
                    (
                        status IN ('pending', 'retryable')
                        AND (
                            retry_eligible_at IS NULL
                            OR retry_eligible_at <= ?
                        )
                    )
                    OR (
                        status = 'processing'
                        AND lease_expires_at <= ?
                        AND retry_eligible_at <= ?
                    )
                  )
                """,
                (
                    now,
                    now,
                    claim_token,
                    lease_expires_at,
                    delivery_id,
                    MAX_ATTEMPTS,
                    now,
                    now,
                    now,
                ),
            )
            return claim_token if cursor.rowcount == 1 else None

    def get_job(self, delivery_id: str) -> dict[str, Any] | None:
        """Retrieve a job by its GitHub delivery identifier."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_jobs WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()

        if row is None:
            return None

        job = dict(row)
        job.pop("claim_token", None)
        return job

    def count_jobs(self) -> int:
        """Return the number of persisted jobs."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM review_jobs"
            ).fetchone()

        return int(row["total"])

    def update_status(
        self,
        delivery_id: str,
        status: str,
        error: str | None = None,
        increment_attempt: bool = False,
        retry_after_seconds: float | None = None,
        claim_token: str | None = None,
    ) -> bool:
        """Update job state, requiring active claim ownership for worker writes."""
        if status not in VALID_STATUSES:
            raise ValueError(
                f"Invalid job status: {status}. "
                f"Expected one of: {sorted(VALID_STATUSES)}"
            )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            timestamp = self._timestamp(now)
            row = connection.execute(
                """
                SELECT attempt_count, status, claim_token, lease_expires_at
                FROM review_jobs
                WHERE delivery_id = ?
                """,
                (delivery_id,),
            ).fetchone()
            if row is None:
                return False

            if status in {"processing", "completed", "retryable", "failed"}:
                if (
                    claim_token is None
                    or row["status"] != "processing"
                    or row["claim_token"] != claim_token
                    or row["lease_expires_at"] is None
                    or row["lease_expires_at"] <= timestamp
                ):
                    return False

            attempt_count = int(row["attempt_count"]) + int(increment_attempt)
            final_status = status
            retry_eligible_at = None
            if status == "retryable":
                if attempt_count >= MAX_ATTEMPTS:
                    final_status = "failed"
                else:
                    delay_index = max(
                        0,
                        min(attempt_count - 1, len(RETRY_DELAYS_SECONDS) - 1),
                    )
                    delay = RETRY_DELAYS_SECONDS[delay_index]
                    if (
                        retry_after_seconds is not None
                        and math.isfinite(retry_after_seconds)
                    ):
                        delay = max(
                            delay,
                            min(
                                MAX_RETRY_AFTER_SECONDS,
                                max(0.0, retry_after_seconds),
                            ),
                        )
                    retry_eligible_at = self._timestamp(
                        now + timedelta(seconds=delay)
                    )

            lease_expires_at = (
                self._timestamp(now + timedelta(seconds=self.lease_seconds))
                if final_status == "processing"
                else None
            )
            cursor = connection.execute(
                """
                UPDATE review_jobs
                SET status = ?,
                    last_error = ?,
                    attempt_count = attempt_count + ?,
                    updated_at = ?,
                    retry_eligible_at = ?,
                    claim_token = ?,
                    lease_expires_at = ?,
                    started_at = CASE
                        WHEN ? = 'processing'
                        THEN COALESCE(started_at, ?)
                        ELSE started_at
                    END,
                    completed_at = CASE
                        WHEN ? IN ('completed', 'failed')
                        THEN ?
                        WHEN ? IN ('pending', 'processing', 'retryable')
                        THEN NULL
                        ELSE completed_at
                    END
                WHERE delivery_id = ?
                  AND (
                    (
                        ? IS NOT NULL
                        AND status = 'processing'
                        AND claim_token = ?
                        AND lease_expires_at > ?
                    )
                    OR (
                        ? IS NULL
                        AND status NOT IN (
                            'processing', 'completed', 'retryable', 'failed'
                        )
                        AND ? NOT IN (
                            'processing', 'completed', 'retryable', 'failed'
                        )
                    )
                  )
                """,
                (
                    final_status,
                    error,
                    int(increment_attempt),
                    timestamp,
                    retry_eligible_at,
                    claim_token if final_status == "processing" else None,
                    lease_expires_at,
                    final_status,
                    timestamp,
                    final_status,
                    timestamp,
                    final_status,
                    delivery_id,
                    claim_token,
                    claim_token,
                    timestamp,
                    claim_token,
                    final_status,
                ),
            )

            return cursor.rowcount == 1

    def save_pull_request_data(
        self,
        delivery_id: str,
        *,
        pull_request_url: str,
        head_sha: str,
        title: str,
        changed_files: list[dict[str, Any]],
        claim_token: str | None = None,
    ) -> bool:
        """Persist PR data only when the caller owns an active claim."""
        serialized_files = json.dumps(changed_files)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = self._timestamp()
            cursor = connection.execute(
                """
                UPDATE review_jobs
                SET pull_request_url = ?,
                    head_sha = ?,
                    title = ?,
                    changed_files_json = ?,
                    updated_at = ?
                WHERE delivery_id = ?
                  AND status = 'processing'
                  AND claim_token = ?
                  AND lease_expires_at > ?
                """,
                (
                    pull_request_url,
                    head_sha,
                    title,
                    serialized_files,
                    now,
                    delivery_id,
                    claim_token,
                    now,
                ),
            )

            return cursor.rowcount == 1