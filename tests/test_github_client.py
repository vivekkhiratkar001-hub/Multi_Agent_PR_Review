from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from github import GithubException
from pydantic import SecretStr

from pr_reviewer.config import Settings
from pr_reviewer.github_client import (
    GitHubClientError,
    GitHubPRClient,
)


def make_settings():
    return Settings(
        _env_file=None,
        app_env="test",
        github_token=SecretStr("test-token"),
    )


def make_pull_request():
    file = SimpleNamespace(
        filename="src/app.py",
        status="modified",
        additions=3,
        deletions=1,
        changes=4,
        patch="@@ -1 +1,3 @@",
    )

    return SimpleNamespace(
        number=42,
        title="Fix validation",
        html_url="https://github.com/example/project/pull/42",
        state="open",
        base=SimpleNamespace(sha="base123"),
        head=SimpleNamespace(sha="head456"),
        get_files=Mock(return_value=[file]),
    )


def test_retrieves_pull_request_metadata_and_files():
    pull = make_pull_request()
    repo = Mock()
    repo.get_pull.return_value = pull

    github = Mock()
    github.get_repo.return_value = repo

    client = GitHubPRClient(
        settings=make_settings(),
        github_factory=Mock(return_value=github),
        sleep=lambda _: None,
    )

    result = client.get_pull_request("example/project", 42)

    assert result["number"] == 42
    assert result["title"] == "Fix validation"
    assert result["head_sha"] == "head456"
    assert result["changed_files"][0]["filename"] == "src/app.py"
    assert result["changed_files"][0]["patch"] == "@@ -1 +1,3 @@"


def test_retries_transient_server_error():
    pull = make_pull_request()
    repo = Mock()
    repo.get_pull.side_effect = [
        GithubException(503, {"message": "Service unavailable"}, {}),
        pull,
    ]

    github = Mock()
    github.get_repo.return_value = repo
    sleep = Mock()

    client = GitHubPRClient(
        settings=make_settings(),
        max_attempts=3,
        base_delay=0.1,
        sleep=sleep,
        github_factory=Mock(return_value=github),
    )

    result = client.get_pull_request("example/project", 42)

    assert result["number"] == 42
    assert repo.get_pull.call_count == 2
    sleep.assert_called_once_with(0.1)


def test_retries_transient_network_error():
    pull = make_pull_request()
    repo = Mock()
    repo.get_pull.side_effect = [
        requests.ConnectionError("Connection reset"),
        pull,
    ]
    github = Mock()
    github.get_repo.return_value = repo
    sleep = Mock()

    client = GitHubPRClient(
        settings=make_settings(),
        max_attempts=2,
        base_delay=0.1,
        sleep=sleep,
        github_factory=Mock(return_value=github),
    )

    result = client.get_pull_request("example/project", 42)

    assert result["number"] == 42
    assert repo.get_pull.call_count == 2
    sleep.assert_called_once_with(0.1)


def test_does_not_retry_permanent_not_found_error():
    repo = Mock()
    repo.get_pull.side_effect = GithubException(
        404, {"message": "Not Found"}, {}
    )

    github = Mock()
    github.get_repo.return_value = repo
    sleep = Mock()

    client = GitHubPRClient(
        settings=make_settings(),
        max_attempts=3,
        sleep=sleep,
        github_factory=Mock(return_value=github),
    )

    with pytest.raises(GitHubClientError, match="404"):
        client.get_pull_request("example/project", 42)

    assert repo.get_pull.call_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize(
    "status",
    [400, 401, 403, 404, 422],
)
def test_permanent_github_responses_are_not_retryable(status):
    repo = Mock()
    repo.get_pull.side_effect = GithubException(
        status,
        {"message": "Request cannot succeed"},
        (
            {"X-RateLimit-Reset": "1760000000", "X-RateLimit-Remaining": "100"}
            if status == 403
            else {}
        ),
    )
    github = Mock()
    github.get_repo.return_value = repo

    client = GitHubPRClient(
        settings=make_settings(),
        github_factory=Mock(return_value=github),
        sleep=Mock(),
    )

    with pytest.raises(GitHubClientError) as error:
        client.get_pull_request("example/project", 42)

    assert error.value.retryable is False
    assert repo.get_pull.call_count == 1


def test_rate_limit_retry_after_is_honored():
    pull = make_pull_request()
    repo = Mock()
    repo.get_pull.side_effect = [
        GithubException(
            403,
            {"message": "API rate limit exceeded"},
            {"Retry-After": "12"},
        ),
        pull,
    ]
    github = Mock()
    github.get_repo.return_value = repo
    sleep = Mock()

    client = GitHubPRClient(
        settings=make_settings(),
        max_attempts=2,
        base_delay=0.1,
        sleep=sleep,
        github_factory=Mock(return_value=github),
    )

    result = client.get_pull_request("example/project", 42)

    assert result["number"] == 42
    sleep.assert_called_once_with(12.0)
    assert repo.get_pull.call_count == 2


def test_extreme_retry_after_is_capped():
    pull = make_pull_request()
    repo = Mock()
    repo.get_pull.side_effect = [
        GithubException(
            429,
            {"message": "Rate limit exceeded"},
            {"Retry-After": "99999"},
        ),
        pull,
    ]
    github = Mock()
    github.get_repo.return_value = repo
    sleep = Mock()

    client = GitHubPRClient(
        settings=make_settings(),
        max_attempts=2,
        sleep=sleep,
        github_factory=Mock(return_value=github),
    )

    client.get_pull_request("example/project", 42)

    sleep.assert_called_once_with(300)


def test_fails_after_retry_limit():
    repo = Mock()
    repo.get_pull.side_effect = GithubException(
        503, {"message": "Service unavailable"}, {}
    )

    github = Mock()
    github.get_repo.return_value = repo
    sleep = Mock()

    client = GitHubPRClient(
        settings=make_settings(),
        max_attempts=3,
        sleep=sleep,
        github_factory=Mock(return_value=github),
    )

    with pytest.raises(GitHubClientError, match="3 attempt"):
        client.get_pull_request("example/project", 42)

    assert repo.get_pull.call_count == 3
    assert sleep.call_count == 2


def test_unknown_github_exception_is_not_retried():
    repo = Mock()
    repo.get_pull.side_effect = RuntimeError("Unexpected client failure")
    github = Mock()
    github.get_repo.return_value = repo

    client = GitHubPRClient(
        settings=make_settings(),
        github_factory=Mock(return_value=github),
        sleep=Mock(),
    )

    with pytest.raises(GitHubClientError) as error:
        client.get_pull_request("example/project", 42)

    assert error.value.retryable is False
    assert repo.get_pull.call_count == 1


def test_missing_token_is_rejected():
    settings = Settings(_env_file=None, app_env="test")

    with pytest.raises(GitHubClientError, match="GITHUB_TOKEN"):
        GitHubPRClient(settings=settings)