
from unittest.mock import Mock
from datetime import datetime, timedelta, timezone

import pytest

from pr_reviewer.github_client import GitHubClientError
from pr_reviewer.jobs import JobStore
from pr_reviewer.processor import ReviewJobProcessor


@pytest.fixture
def setup_processor(tmp_path):
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    store = JobStore(tmp_path / "jobs.sqlite3", clock=lambda: now)
    store.record_job(
        delivery_id="delivery-001",
        repository="example/project",
        pull_request_number=42,
        action="opened",
    )

    github_client = Mock()
    github_client.get_pull_request.return_value = {
        "url": "https://github.com/example/project/pull/42",
        "head_sha": "head123",
        "title": "Fix validation",
        "changed_files": [
            {
                "filename": "src/app.py",
                "status": "modified",
                "patch": "@@ -1 +1 @@",
            }
        ],
    }

    processor = ReviewJobProcessor(store, github_client)
    return processor, store, github_client


def test_processor_saves_pr_data_and_completes_job(setup_processor):
    processor, store, github_client = setup_processor

    result = processor.process("delivery-001")

    assert result["status"] == "completed"
    assert result["attempt_count"] == 1
    assert result["head_sha"] == "head123"
    assert result["title"] == "Fix validation"
    assert result["pull_request_url"].endswith("/pull/42")
    assert github_client.get_pull_request.call_count == 1


def test_processor_does_not_reprocess_completed_job(setup_processor):
    processor, store, github_client = setup_processor

    processor.process("delivery-001")
    result = processor.process("delivery-001")

    assert result["status"] == "completed"
    assert github_client.get_pull_request.call_count == 1


def test_processor_does_not_process_job_with_unexpired_claim(setup_processor):
    processor, store, github_client = setup_processor
    assert store.claim_job_with_token("delivery-001") is not None

    with pytest.raises(RuntimeError, match="current status: processing"):
        processor.process("delivery-001")

    github_client.get_pull_request.assert_not_called()


def test_old_processor_cannot_overwrite_reclaimed_job(tmp_path):
    now = [datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)]
    store = JobStore(
        tmp_path / "processor-lease.sqlite3",
        clock=lambda: now[0],
        lease_seconds=10,
    )
    store.record_job("delivery-lease", "example/project", 42, "opened")
    github_client = Mock()
    processor = ReviewJobProcessor(store, github_client)

    def fetch_old_attempt(*_):
        now[0] += timedelta(seconds=15)
        new_token = store.claim_job_with_token("delivery-lease")
        assert new_token is not None
        assert store.save_pull_request_data(
            "delivery-lease",
            pull_request_url="https://github.com/example/project/pull/42",
            head_sha="new-head",
            title="New attempt",
            changed_files=[],
            claim_token=new_token,
        )
        assert store.update_status(
            "delivery-lease",
            "completed",
            claim_token=new_token,
        )
        return {
            "url": "https://github.com/example/project/pull/42",
            "head_sha": "old-head",
            "title": "Old attempt",
            "changed_files": [],
        }

    github_client.get_pull_request.side_effect = fetch_old_attempt

    with pytest.raises(RuntimeError, match="Could not persist PR data"):
        processor.process("delivery-lease")

    job = store.get_job("delivery-lease")
    assert job["status"] == "completed"
    assert job["head_sha"] == "new-head"
    assert job["title"] == "New attempt"


def test_processor_records_github_failure_as_retryable(setup_processor):
    processor, store, github_client = setup_processor
    github_client.get_pull_request.side_effect = GitHubClientError(
        "GitHub API operation failed after 3 attempts",
        retryable=True,
    )

    with pytest.raises(GitHubClientError):
        processor.process("delivery-001")

    job = store.get_job("delivery-001")

    assert job["status"] == "retryable"
    assert job["attempt_count"] == 1
    assert "3 attempts" in job["last_error"]
    assert job["retry_eligible_at"] is not None


def test_processor_fails_unknown_github_error_without_retry(setup_processor):
    processor, store, github_client = setup_processor
    github_client.get_pull_request.side_effect = GitHubClientError(
        "Unknown GitHub failure"
    )

    with pytest.raises(GitHubClientError):
        processor.process("delivery-001")

    job = store.get_job("delivery-001")
    assert job["status"] == "failed"
    assert job["attempt_count"] == 1
    assert job["retry_eligible_at"] is None


def test_processor_persists_retry_after_for_rate_limit(setup_processor):
    processor, store, github_client = setup_processor
    github_client.get_pull_request.side_effect = GitHubClientError(
        "Rate limited",
        retryable=True,
        retry_after_seconds=30,
    )

    with pytest.raises(GitHubClientError):
        processor.process("delivery-001")

    job = store.get_job("delivery-001")
    assert job["status"] == "retryable"
    assert job["retry_eligible_at"] == "2026-01-02 03:04:35"


def test_processor_rejects_unknown_delivery(setup_processor):
    processor, _, _ = setup_processor

    with pytest.raises(ValueError, match="Unknown delivery ID"):
        processor.process("does-not-exist")
