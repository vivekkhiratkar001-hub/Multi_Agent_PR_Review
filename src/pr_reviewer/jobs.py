import json
import sqlite3
from pathlib import Path
from typing import Any


VALID_STATUSES = {
    "pending",
    "processing",
    "completed",
    "failed",
    "retryable",
}


class JobStore:
    """Persist review jobs, prevent duplicate deliveries, and track recovery."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

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
                SET updated_at = created_at
                WHERE updated_at IS NULL
                """
            )

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
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO review_jobs
                    (delivery_id, repository, pull_request_number, action)
                VALUES (?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    repository,
                    pull_request_number,
                    action,
                ),
            )
            return cursor.rowcount == 1
    def claim_job(self, delivery_id: str) -> bool:
        """Atomically claim a pending or retryable job for processing."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE review_jobs
                SET status = 'processing',
                    attempt_count = attempt_count + 1,
                    last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP,
                    started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                    completed_at = NULL
                WHERE delivery_id = ?
                  AND status IN ('pending', 'retryable')
                """,
                (delivery_id,),
            )
            return cursor.rowcount == 1
    def get_job(self, delivery_id: str) -> dict[str, Any] | None:
        """Retrieve a job by its GitHub delivery identifier."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_jobs WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()

        return dict(row) if row else None

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
    ) -> bool:
        """Update job lifecycle state and optional failure diagnostics."""
        if status not in VALID_STATUSES:
            raise ValueError(
                f"Invalid job status: {status}. "
                f"Expected one of: {sorted(VALID_STATUSES)}"
            )

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE review_jobs
                SET status = ?,
                    last_error = ?,
                    attempt_count = attempt_count + ?,
                    updated_at = CURRENT_TIMESTAMP,
                    started_at = CASE
                        WHEN ? = 'processing'
                        THEN COALESCE(started_at, CURRENT_TIMESTAMP)
                        ELSE started_at
                    END,
                    completed_at = CASE
                        WHEN ? IN ('completed', 'failed')
                        THEN CURRENT_TIMESTAMP
                        WHEN ? IN ('pending', 'processing', 'retryable')
                        THEN NULL
                        ELSE completed_at
                    END
                WHERE delivery_id = ?
                """,
                (
                    status,
                    error,
                    int(increment_attempt),
                    status,
                    status,
                    status,
                    delivery_id,
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
    ) -> bool:
        """Persist retrieved PR metadata and changed-file information."""
        serialized_files = json.dumps(changed_files)

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE review_jobs
                SET pull_request_url = ?,
                    head_sha = ?,
                    title = ?,
                    changed_files_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ?
                """,
                (
                    pull_request_url,
                    head_sha,
                    title,
                    serialized_files,
                    delivery_id,
                ),
            )

            return cursor.rowcount == 1