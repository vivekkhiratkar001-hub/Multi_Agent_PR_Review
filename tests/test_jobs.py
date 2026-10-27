
import sqlite3
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

from pr_reviewer.jobs import JobStore


def test_record_job_and_retrieve_it(tmp_path):
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    store = JobStore(tmp_path / "jobs.sqlite3", clock=lambda: now)

    created = store.record_job(
        delivery_id="delivery-001",
        repository="example/project",
        pull_request_number=42,
        action="opened",
    )

    assert created is True

    job = store.get_job("delivery-001")

    assert job is not None
    assert job["repository"] == "example/project"
    assert job["pull_request_number"] == 42
    assert job["status"] == "pending"
    assert job["updated_at"] == "2026-01-02 03:04:05"


def test_duplicate_delivery_does_not_create_another_job(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")

    arguments = {
        "delivery_id": "delivery-001",
        "repository": "example/project",
        "pull_request_number": 42,
        "action": "opened",
    }

    assert store.record_job(**arguments) is True
    assert store.record_job(**arguments) is False
    assert store.count_jobs() == 1


def test_job_persists_after_reopening_store(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    first_store = JobStore(db_path)

    first_store.record_job(
        delivery_id="delivery-001",
        repository="example/project",
        pull_request_number=42,
        action="opened",
    )

    second_store = JobStore(db_path)

    assert second_store.count_jobs() == 1
    assert second_store.get_job("delivery-001") is not None


def test_existing_database_is_migrated_without_losing_jobs(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE review_jobs (
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

        connection.execute(
            """
            INSERT INTO review_jobs
                (delivery_id, repository, pull_request_number, action)
            VALUES (?, ?, ?, ?)
            """,
            ("legacy-001", "example/project", 7, "opened"),
        )

    store = JobStore(db_path)

    job = store.get_job("legacy-001")

    assert job is not None
    assert job["repository"] == "example/project"
    assert job["pull_request_number"] == 7
    assert job["retry_eligible_at"] is None
    assert "claim_token" not in job
    assert store.count_jobs() == 1


def test_existing_processing_job_gets_lease_without_resetting_status(tmp_path):
    db_path = tmp_path / "legacy-processing.sqlite3"
    created_at = "2026-01-02 03:04:05"

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE review_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT NOT NULL UNIQUE,
                repository TEXT NOT NULL,
                pull_request_number INTEGER NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                pull_request_url TEXT,
                head_sha TEXT,
                title TEXT,
                changed_files_json TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO review_jobs
                (delivery_id, repository, pull_request_number, action,
                 status, created_at, pull_request_url, head_sha, title,
                 changed_files_json)
            VALUES (?, ?, ?, ?, 'processing', ?, ?, ?, ?, ?)
            """,
            (
                "legacy-processing",
                "example/project",
                7,
                "opened",
                created_at,
                "https://github.com/example/project/pull/7",
                "legacy-head",
                "Legacy title",
                '[{"filename":"legacy.py"}]',
            ),
        )

    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    store = JobStore(
        db_path,
        clock=lambda: now,
        lease_seconds=10,
    )

    job = store.get_job("legacy-processing")
    assert job is not None
    assert job["status"] == "processing"
    assert job["attempt_count"] == 1
    assert job["lease_expires_at"] == "2026-01-02 03:04:15"
    assert job["pull_request_url"] == "https://github.com/example/project/pull/7"
    assert job["head_sha"] == "legacy-head"
    assert job["title"] == "Legacy title"
    assert job["changed_files_json"] == '[{"filename":"legacy.py"}]'
    assert store.claim_job_with_token("legacy-processing") is None


def test_legacy_processing_attempt_limit_survives_reopen(tmp_path):
    db_path = tmp_path / "legacy-attempt-limit.sqlite3"
    created_at = "2026-01-02 03:04:05"

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE review_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT NOT NULL UNIQUE,
                repository TEXT NOT NULL,
                pull_request_number INTEGER NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                pull_request_url TEXT,
                head_sha TEXT,
                title TEXT,
                changed_files_json TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO review_jobs
                (delivery_id, repository, pull_request_number, action,
                 status, created_at, pull_request_url, head_sha, title,
                 changed_files_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "legacy-processing",
                    "example/project",
                    7,
                    "opened",
                    "processing",
                    created_at,
                    "https://github.com/example/project/pull/7",
                    "legacy-head",
                    "Legacy title",
                    '[{"filename":"legacy.py"}]',
                ),
                (
                    "legacy-pending",
                    "example/project",
                    8,
                    "opened",
                    "pending",
                    created_at,
                    None,
                    None,
                    None,
                    None,
                ),
            ],
        )

    now = [datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)]
    store = JobStore(
        db_path,
        clock=lambda: now[0],
        lease_seconds=10,
    )
    job = store.get_job("legacy-processing")
    assert job is not None
    assert job["attempt_count"] == 1

    reopened_store = JobStore(
        db_path,
        clock=lambda: now[0],
        lease_seconds=10,
    )
    assert reopened_store.get_job("legacy-processing")["attempt_count"] == 1
    assert reopened_store.get_job("legacy-pending")["attempt_count"] == 0

    now[0] += timedelta(seconds=10)
    assert reopened_store.claim_job_with_token("legacy-processing") is None
    now[0] += timedelta(seconds=5)
    second_token = reopened_store.claim_job_with_token("legacy-processing")
    assert second_token is not None
    assert reopened_store.get_job("legacy-processing")["attempt_count"] == 2

    now[0] += timedelta(seconds=10)
    assert reopened_store.claim_job_with_token("legacy-processing") is None
    now[0] += timedelta(seconds=15)
    third_token = reopened_store.claim_job_with_token("legacy-processing")
    assert third_token is not None
    assert reopened_store.get_job("legacy-processing")["attempt_count"] == 3

    now[0] += timedelta(seconds=10)
    assert reopened_store.claim_job_with_token("legacy-processing") is None
    job = reopened_store.get_job("legacy-processing")
    assert job["status"] == "failed"
    assert job["attempt_count"] == 3
    assert job["pull_request_url"] == "https://github.com/example/project/pull/7"
    assert job["head_sha"] == "legacy-head"
    assert job["title"] == "Legacy title"
    assert job["changed_files_json"] == '[{"filename":"legacy.py"}]'


def test_existing_attempt_counts_are_not_changed_by_migration(tmp_path):
    db_path = tmp_path / "attempt-count-present.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE review_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT NOT NULL UNIQUE,
                repository TEXT NOT NULL,
                pull_request_number INTEGER NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                retry_eligible_at TEXT,
                pull_request_url TEXT,
                head_sha TEXT,
                title TEXT,
                changed_files_json TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO review_jobs
                (delivery_id, repository, pull_request_number, action,
                 status, created_at, attempt_count, retry_eligible_at,
                 pull_request_url, head_sha, title, changed_files_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "existing-zero-count",
                    "example/project",
                    9,
                    "opened",
                    "processing",
                    "2026-01-02 03:04:05",
                    0,
                    None,
                    "https://github.com/example/project/pull/9",
                    "legacy-zero-head",
                    "Legacy zero title",
                    '[{"filename":"zero.py"}]',
                ),
                (
                    "existing-nonzero-count",
                    "example/project",
                    10,
                    "opened",
                    "processing",
                    "2026-01-02 03:04:05",
                    2,
                    "2026-01-02 03:10:00",
                    "https://github.com/example/project/pull/10",
                    "legacy-nonzero-head",
                    "Legacy nonzero title",
                    '[{"filename":"nonzero.py"}]',
                ),
            ],
        )

    now = [datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)]
    store = JobStore(
        db_path,
        clock=lambda: now[0],
        lease_seconds=10,
    )
    zero_count_job = store.get_job("existing-zero-count")
    assert zero_count_job["attempt_count"] == 1
    assert zero_count_job["pull_request_url"] == (
        "https://github.com/example/project/pull/9"
    )
    assert zero_count_job["head_sha"] == "legacy-zero-head"
    assert zero_count_job["title"] == "Legacy zero title"
    assert zero_count_job["changed_files_json"] == '[{"filename":"zero.py"}]'

    nonzero_count_job = store.get_job("existing-nonzero-count")
    assert nonzero_count_job["attempt_count"] == 2
    assert nonzero_count_job["retry_eligible_at"] == "2026-01-02 03:10:00"
    assert nonzero_count_job["pull_request_url"] == (
        "https://github.com/example/project/pull/10"
    )
    assert nonzero_count_job["head_sha"] == "legacy-nonzero-head"
    assert nonzero_count_job["title"] == "Legacy nonzero title"
    assert nonzero_count_job["changed_files_json"] == '[{"filename":"nonzero.py"}]'

    reopened_store = JobStore(
        db_path,
        clock=lambda: now[0],
        lease_seconds=10,
    )
    assert reopened_store.get_job("existing-zero-count")["attempt_count"] == 1
    assert reopened_store.get_job("existing-nonzero-count")["attempt_count"] == 2

    now[0] += timedelta(seconds=10)
    assert reopened_store.claim_job_with_token("existing-zero-count") is None
    now[0] += timedelta(seconds=5)
    second_token = reopened_store.claim_job_with_token("existing-zero-count")
    assert second_token is not None
    assert reopened_store.get_job("existing-zero-count")["attempt_count"] == 2

    now[0] += timedelta(seconds=10)
    assert reopened_store.claim_job_with_token("existing-zero-count") is None
    now[0] += timedelta(seconds=15)
    third_token = reopened_store.claim_job_with_token("existing-zero-count")
    assert third_token is not None
    assert reopened_store.get_job("existing-zero-count")["attempt_count"] == 3

    now[0] += timedelta(seconds=10)
    assert reopened_store.claim_job_with_token("existing-zero-count") is None
    exhausted_job = reopened_store.get_job("existing-zero-count")
    assert exhausted_job["status"] == "failed"
    assert exhausted_job["attempt_count"] == 3
    assert exhausted_job["pull_request_url"] == (
        "https://github.com/example/project/pull/9"
    )
def test_job_tracks_processing_attempts_and_failure(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")

    store.record_job(
        delivery_id="delivery-002",
        repository="example/project",
        pull_request_number=42,
        action="opened",
    )

    claim_token = store.claim_job_with_token("delivery-002")
    assert claim_token is not None

    assert store.update_status(
        "delivery-002",
        "retryable",
        error="Temporary GitHub API error",
        claim_token=claim_token,
    )

    job = store.get_job("delivery-002")

    assert job["status"] == "retryable"
    assert job["attempt_count"] == 1
    assert job["last_error"] == "Temporary GitHub API error"
    assert job["started_at"] is not None


def test_job_stores_pull_request_data(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")

    store.record_job(
        delivery_id="delivery-003",
        repository="example/project",
        pull_request_number=42,
        action="opened",
    )

    changed_files = [
        {
            "filename": "src/app.py",
            "status": "modified",
            "patch": "@@ -1 +1 @@",
        }
    ]

    claim_token = store.claim_job_with_token("delivery-003")
    assert claim_token is not None
    assert store.save_pull_request_data(
        "delivery-003",
        pull_request_url="https://github.com/example/project/pull/42",
        head_sha="abc123",
        title="Fix validation",
        changed_files=changed_files,
        claim_token=claim_token,
    )

    job = store.get_job("delivery-003")

    assert job["head_sha"] == "abc123"
    assert job["title"] == "Fix validation"
    assert json.loads(job["changed_files_json"]) == changed_files


def test_invalid_job_status_is_rejected(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")

    try:
        store.update_status("delivery-004", "unknown")
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid job status should raise ValueError")
def test_claim_job_changes_pending_to_processing(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.record_job("delivery-005", "example/project", 42, "opened")

    assert store.claim_job("delivery-005") is True

    job = store.get_job("delivery-005")
    assert job["status"] == "processing"
    assert job["attempt_count"] == 1
    assert job["started_at"] is not None


def test_claim_job_cannot_claim_the_same_job_twice(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.record_job("delivery-006", "example/project", 42, "opened")

    assert store.claim_job("delivery-006") is True
    assert store.claim_job("delivery-006") is False

    job = store.get_job("delivery-006")
    assert job["status"] == "processing"
    assert job["attempt_count"] == 1


def test_simultaneous_claims_have_exactly_one_winner(tmp_path):
    db_path = tmp_path / "concurrent.sqlite3"
    store = JobStore(db_path)
    store.record_job("delivery-concurrent", "example/project", 42, "opened")

    def claim():
        return JobStore(db_path).claim_job_with_token("delivery-concurrent")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))

    assert sum(token is not None for token in results) == 1
    job = store.get_job("delivery-concurrent")
    assert job["attempt_count"] == 1
    assert job["status"] == "processing"


def test_expired_processing_lease_can_be_reclaimed(tmp_path):
    now = [datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)]
    store = JobStore(
        tmp_path / "lease.sqlite3",
        clock=lambda: now[0],
        lease_seconds=10,
    )
    store.record_job("delivery-lease", "example/project", 42, "opened")

    first_token = store.claim_job_with_token("delivery-lease")
    assert first_token is not None
    assert store.claim_job_with_token("delivery-lease") is None

    now[0] += timedelta(seconds=10)
    assert store.claim_job_with_token("delivery-lease") is None
    now[0] += timedelta(seconds=5)
    second_token = store.claim_job_with_token("delivery-lease")
    assert second_token is not None
    assert second_token != first_token

    job = store.get_job("delivery-lease")
    assert job["status"] == "processing"
    assert job["attempt_count"] == 2
    assert job["lease_expires_at"] == "2026-01-02 03:04:30"


def test_crash_recovery_backoff_is_persisted_and_not_extended(tmp_path):
    now = [datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)]
    db_path = tmp_path / "recovery-backoff.sqlite3"
    store = JobStore(
        db_path,
        clock=lambda: now[0],
        lease_seconds=10,
    )
    store.record_job("delivery-backoff", "example/project", 42, "opened")

    first_token = store.claim_job_with_token("delivery-backoff")
    assert first_token is not None
    now[0] += timedelta(seconds=10)

    assert store.claim_job_with_token("delivery-backoff") is None
    job = store.get_job("delivery-backoff")
    assert job["retry_eligible_at"] == "2026-01-02 03:04:20"

    now[0] += timedelta(seconds=4)
    reopened_store = JobStore(
        db_path,
        clock=lambda: now[0],
        lease_seconds=10,
    )
    assert reopened_store.claim_job_with_token("delivery-backoff") is None
    assert (
        reopened_store.get_job("delivery-backoff")["retry_eligible_at"]
        == "2026-01-02 03:04:20"
    )

    now[0] += timedelta(seconds=1)
    second_token = reopened_store.claim_job_with_token("delivery-backoff")
    assert second_token is not None
    assert reopened_store.get_job("delivery-backoff")["attempt_count"] == 2

    now[0] += timedelta(seconds=10)
    assert reopened_store.claim_job_with_token("delivery-backoff") is None
    assert (
        reopened_store.get_job("delivery-backoff")["retry_eligible_at"]
        == "2026-01-02 03:04:45"
    )
    now[0] += timedelta(seconds=14)
    assert reopened_store.claim_job_with_token("delivery-backoff") is None
    now[0] += timedelta(seconds=1)
    third_token = reopened_store.claim_job_with_token("delivery-backoff")
    assert third_token is not None
    assert reopened_store.get_job("delivery-backoff")["attempt_count"] == 3


def test_competing_workers_reclaim_expired_lease_only_once(tmp_path):
    now = [datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)]
    db_path = tmp_path / "competing-recovery.sqlite3"
    store = JobStore(
        db_path,
        clock=lambda: now[0],
        lease_seconds=10,
    )
    store.record_job("delivery-race", "example/project", 42, "opened")
    old_token = store.claim_job_with_token("delivery-race")
    assert old_token is not None

    now[0] += timedelta(seconds=10)
    assert store.claim_job_with_token("delivery-race") is None
    now[0] += timedelta(seconds=5)
    barrier = Barrier(2)

    def competing_claim():
        barrier.wait()
        return JobStore(
            db_path,
            clock=lambda: now[0],
            lease_seconds=10,
        ).claim_job_with_token("delivery-race")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: competing_claim(), range(2)))

    winners = [token for token in results if token is not None]
    losers = [token for token in results if token is None]
    assert len(winners) == 1
    assert losers == [None]
    winning_token = winners[0]

    with sqlite3.connect(db_path) as connection:
        current_token = connection.execute(
            "SELECT claim_token FROM review_jobs WHERE delivery_id = ?",
            ("delivery-race",),
        ).fetchone()[0]
    assert current_token == winning_token

    assert store.save_pull_request_data(
        "delivery-race",
        pull_request_url="https://github.com/example/project/pull/42",
        head_sha="winner-head",
        title="Winner",
        changed_files=[],
        claim_token=winning_token,
    )
    assert store.update_status(
        "delivery-race",
        "completed",
        claim_token=winning_token,
    )

    for stale_token in [old_token, losers[0]]:
        assert not store.save_pull_request_data(
            "delivery-race",
            pull_request_url="https://github.com/example/project/pull/42",
            head_sha="stale-head",
            title="Stale worker",
            changed_files=[],
            claim_token=stale_token,
        )
        assert not store.update_status(
            "delivery-race",
            "failed",
            error="Stale worker failure",
            claim_token=stale_token,
        )

    job = store.get_job("delivery-race")
    assert job["status"] == "completed"
    assert job["attempt_count"] == 2
    assert job["head_sha"] == "winner-head"
    assert job["title"] == "Winner"


def test_lost_claim_cannot_save_data_or_update_status(tmp_path):
    now = [datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)]
    store = JobStore(
        tmp_path / "fenced.sqlite3",
        clock=lambda: now[0],
        lease_seconds=10,
    )
    store.record_job("delivery-fenced", "example/project", 42, "opened")
    old_token = store.claim_job_with_token("delivery-fenced")
    assert old_token is not None

    now[0] += timedelta(seconds=15)
    new_token = store.claim_job_with_token("delivery-fenced")
    assert new_token is not None

    assert store.save_pull_request_data(
        "delivery-fenced",
        pull_request_url="https://github.com/example/project/pull/42",
        head_sha="new-head",
        title="New attempt",
        changed_files=[],
        claim_token=new_token,
    )
    assert not store.save_pull_request_data(
        "delivery-fenced",
        pull_request_url="https://github.com/example/project/pull/42",
        head_sha="old-head",
        title="Old attempt",
        changed_files=[],
        claim_token=old_token,
    )
    assert not store.update_status(
        "delivery-fenced",
        "completed",
        claim_token=old_token,
    )
    assert not store.update_status(
        "delivery-fenced",
        "retryable",
        error="Old attempt failed",
        claim_token=old_token,
    )

    job = store.get_job("delivery-fenced")
    assert job["status"] == "processing"
    assert job["head_sha"] == "new-head"
    assert job["title"] == "New attempt"


def test_expired_processing_job_at_attempt_limit_fails(tmp_path):
    now = [datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)]
    store = JobStore(
        tmp_path / "exhausted-lease.sqlite3",
        clock=lambda: now[0],
        lease_seconds=10,
    )
    store.record_job("delivery-exhausted", "example/project", 42, "opened")

    for attempt in range(3):
        token = store.claim_job_with_token("delivery-exhausted")
        assert token is not None
        if attempt == 0:
            now[0] += timedelta(seconds=10)
            assert store.claim_job_with_token("delivery-exhausted") is None
            now[0] += timedelta(seconds=5)
        elif attempt == 1:
            now[0] += timedelta(seconds=10)
            assert store.claim_job_with_token("delivery-exhausted") is None
            now[0] += timedelta(seconds=15)

    now[0] += timedelta(seconds=10)
    assert store.claim_job_with_token("delivery-exhausted") is None
    job = store.get_job("delivery-exhausted")
    assert job["status"] == "failed"
    assert job["attempt_count"] == 3
    assert "claim_token" not in job
    assert job["lease_expires_at"] is None


def test_claim_job_can_reclaim_retryable_job(tmp_path):
    now = [datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)]
    store = JobStore(tmp_path / "jobs.sqlite3", clock=lambda: now[0])
    store.record_job("delivery-007", "example/project", 42, "opened")

    claim_token = store.claim_job_with_token("delivery-007")
    assert claim_token is not None
    assert store.update_status(
        "delivery-007",
        "retryable",
        error="Temporary API failure",
        claim_token=claim_token,
    )

    assert store.claim_job("delivery-007") is False
    now[0] += timedelta(seconds=5)
    assert store.claim_job("delivery-007") is True

    job = store.get_job("delivery-007")
    assert job["status"] == "processing"
    assert job["attempt_count"] == 2
    assert job["last_error"] is None


def test_retry_schedule_enforces_five_and_fifteen_second_delays(tmp_path):
    now = [datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)]
    store = JobStore(tmp_path / "jobs.sqlite3", clock=lambda: now[0])
    store.record_job("delivery-008", "example/project", 42, "opened")

    claim_token = store.claim_job_with_token("delivery-008")
    assert claim_token is not None
    assert store.update_status(
        "delivery-008",
        "retryable",
        error="Temporary failure",
        claim_token=claim_token,
    )
    job = store.get_job("delivery-008")
    assert job["retry_eligible_at"] == "2026-01-02 03:04:10"
    assert not store.claim_job("delivery-008")

    now[0] += timedelta(seconds=5)
    claim_token = store.claim_job_with_token("delivery-008")
    assert claim_token is not None
    assert store.get_job("delivery-008")["attempt_count"] == 2
    assert store.update_status(
        "delivery-008",
        "retryable",
        error="Temporary failure",
        claim_token=claim_token,
    )
    job = store.get_job("delivery-008")
    assert job["retry_eligible_at"] == "2026-01-02 03:04:25"
    assert not store.claim_job("delivery-008")

    now[0] += timedelta(seconds=15)
    claim_token = store.claim_job_with_token("delivery-008")
    assert claim_token is not None
    job = store.get_job("delivery-008")
    assert job["attempt_count"] == 3
    assert job["retry_eligible_at"] is None


def test_retry_after_extends_job_retry_eligibility(tmp_path):
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    store = JobStore(tmp_path / "jobs.sqlite3", clock=lambda: now)
    store.record_job("delivery-009", "example/project", 42, "opened")
    claim_token = store.claim_job_with_token("delivery-009")
    assert claim_token is not None

    assert store.update_status(
        "delivery-009",
        "retryable",
        error="Rate limited",
        retry_after_seconds=1000,
        claim_token=claim_token,
    )

    assert (
        store.get_job("delivery-009")["retry_eligible_at"]
        == "2026-01-02 03:09:05"
    )


def test_third_failed_attempt_transitions_to_failed(tmp_path):
    now = [datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)]
    store = JobStore(tmp_path / "jobs.sqlite3", clock=lambda: now[0])
    store.record_job("delivery-010", "example/project", 42, "opened")

    for attempt in range(3):
        claim_token = store.claim_job_with_token("delivery-010")
        assert claim_token is not None
        assert store.update_status(
            "delivery-010",
            "retryable",
            error=f"failure {attempt + 1}",
            claim_token=claim_token,
        )
        if attempt < 2:
            job = store.get_job("delivery-010")
            assert job["status"] == "retryable"
            assert job["attempt_count"] == attempt + 1
            now[0] += timedelta(seconds=5 if attempt == 0 else 15)
        else:
            job = store.get_job("delivery-010")
            assert job["status"] == "failed"
            assert job["attempt_count"] == 3
            assert job["retry_eligible_at"] is None
            assert job["completed_at"] is not None

    assert not store.claim_job("delivery-010")


def test_claim_marks_legacy_exhausted_job_failed(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    store = JobStore(db_path)
    store.record_job("delivery-011", "example/project", 42, "opened")

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE review_jobs
            SET status = 'retryable', attempt_count = 3
            WHERE delivery_id = ?
            """,
            ("delivery-011",),
        )

    assert not store.claim_job("delivery-011")
    job = store.get_job("delivery-011")
    assert job["status"] == "failed"
    assert job["completed_at"] is not None
    assert job["retry_eligible_at"] is None
