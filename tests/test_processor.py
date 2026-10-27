
from unittest.mock import Mock

import pytest

from pr_reviewer.github_client import GitHubClientError
from pr_reviewer.jobs import JobStore
from pr_reviewer.processor import ReviewJobProcessor


@pytest.fixture
def setup_processor(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
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


def test_processor_records_github_failure_as_retryable(setup_processor):
    processor, store, github_client = setup_processor
    github_client.get_pull_request.side_effect = GitHubClientError(
        "GitHub API operation failed after 3 attempts"
    )

    with pytest.raises(GitHubClientError):
        processor.process("delivery-001")

    job = store.get_job("delivery-001")

    assert job["status"] == "retryable"
    assert job["attempt_count"] == 1
    assert "3 attempts" in job["last_error"]


def test_processor_rejects_unknown_delivery(setup_processor):
    processor, _, _ = setup_processor

    with pytest.raises(ValueError, match="Unknown delivery ID"):
        processor.process("does-not-exist")
