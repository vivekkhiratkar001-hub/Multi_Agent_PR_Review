
import sqlite3
import json

from pr_reviewer.jobs import JobStore


def test_record_job_and_retrieve_it(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")

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
    assert store.count_jobs() == 1
def test_job_tracks_processing_attempts_and_failure(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")

    store.record_job(
        delivery_id="delivery-002",
        repository="example/project",
        pull_request_number=42,
        action="opened",
    )

    assert store.update_status(
        "delivery-002",
        "processing",
        increment_attempt=True,
    )

    assert store.update_status(
        "delivery-002",
        "retryable",
        error="Temporary GitHub API error",
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

    assert store.save_pull_request_data(
        "delivery-003",
        pull_request_url="https://github.com/example/project/pull/42",
        head_sha="abc123",
        title="Fix validation",
        changed_files=changed_files,
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


def test_claim_job_can_reclaim_retryable_job(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.record_job("delivery-007", "example/project", 42, "opened")

    assert store.claim_job("delivery-007") is True
    assert store.update_status(
        "delivery-007",
        "retryable",
        error="Temporary API failure",
    )

    assert store.claim_job("delivery-007") is True

    job = store.get_job("delivery-007")
    assert job["status"] == "processing"
    assert job["attempt_count"] == 2
    assert job["last_error"] is None
