import math
import time
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import requests
from github import Github, GithubException

from pr_reviewer.config import Settings


MAX_RETRY_DELAY_SECONDS = 300


class GitHubClientError(RuntimeError):
    """Raised when GitHub PR data cannot be retrieved."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class GitHubPRClient:
    """Retrieve GitHub pull-request metadata and changed files."""

    RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        settings: Settings,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        github_factory: Callable[..., Any] = Github,
        clock: Callable[[], float] = time.time,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        if base_delay < 0:
            raise ValueError("base_delay must not be negative")

        token = settings.github_token
        if token is None or not token.get_secret_value().strip():
            raise GitHubClientError(
                "GitHub token is not configured. Set GITHUB_TOKEN."
            )

        self.github = github_factory(
            login_or_token=token.get_secret_value()
        )
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.sleep = sleep
        self.clock = clock

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """Determine whether an API failure may succeed on retry."""
        if isinstance(exc, GithubException):
            if exc.status in GitHubPRClient.RETRYABLE_STATUSES:
                return True

            # GitHub may return 403 when its API rate limit is exceeded.
            if exc.status == 403:
                details = str(exc.data).lower()
                return (
                    "rate limit" in details
                    or "secondary rate limit" in details
                    or "abuse detection" in details
                    or GitHubPRClient._header(exc, "retry-after") is not None
                    or GitHubPRClient._header(
                        exc, "x-ratelimit-remaining"
                    ) == "0"
                )

            return False

        if isinstance(exc, requests.RequestException):
            return True

        return False

    @staticmethod
    def _header(exc: GithubException, name: str) -> str | None:
        headers = getattr(exc, "headers", None) or {}
        for key, value in headers.items():
            if str(key).lower() == name:
                return str(value)
        return None

    def _retry_after_seconds(self, exc: Exception) -> float | None:
        if not isinstance(exc, GithubException):
            return None

        retry_after = self._header(exc, "retry-after")
        if retry_after is not None:
            try:
                seconds = float(retry_after)
                return (
                    min(MAX_RETRY_DELAY_SECONDS, max(0.0, seconds))
                    if math.isfinite(seconds)
                    else None
                )
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after).timestamp()
                    return min(
                        MAX_RETRY_DELAY_SECONDS,
                        max(0.0, retry_at - self.clock()),
                    )
                except (TypeError, ValueError, OverflowError):
                    pass

        reset_at = self._header(exc, "x-ratelimit-reset")
        if reset_at is not None:
            try:
                seconds = float(reset_at) - self.clock()
                return (
                    min(MAX_RETRY_DELAY_SECONDS, max(0.0, seconds))
                    if math.isfinite(seconds)
                    else None
                )
            except ValueError:
                pass

        return None

    def _execute(self, operation: Callable[[], Any]) -> Any:
        """Run an API operation with a bounded exponential-backoff retry."""
        for attempt in range(1, self.max_attempts + 1):
            try:
                return operation()
            except Exception as exc:
                retryable = self._is_retryable(exc)
                retry_after = self._retry_after_seconds(exc)
                if (
                    attempt >= self.max_attempts
                    or not retryable
                ):
                    raise GitHubClientError(
                        f"GitHub API operation failed after "
                        f"{attempt} attempt(s): {exc}",
                        retryable=retryable,
                        retry_after_seconds=retry_after,
                    ) from exc

                delay = self.base_delay * (2 ** (attempt - 1))
                if retry_after is not None:
                    delay = max(delay, retry_after)
                delay = min(MAX_RETRY_DELAY_SECONDS, delay)
                self.sleep(delay)

        raise GitHubClientError("GitHub API operation failed")

    def get_pull_request(
        self,
        repository: str,
        pull_request_number: int,
    ) -> dict[str, Any]:
        """Retrieve PR metadata and its changed files."""
        if not repository.strip():
            raise ValueError("Repository name must not be empty")

        if pull_request_number < 1:
            raise ValueError("Pull request number must be positive")

        def fetch() -> dict[str, Any]:
            repo = self.github.get_repo(repository)
            pull = repo.get_pull(pull_request_number)

            changed_files = []
            for file in pull.get_files():
                changed_files.append(
                    {
                        "filename": file.filename,
                        "status": file.status,
                        "additions": file.additions,
                        "deletions": file.deletions,
                        "changes": file.changes,
                        "patch": getattr(file, "patch", None),
                    }
                )

            return {
                "repository": repository,
                "number": pull.number,
                "title": pull.title,
                "url": pull.html_url,
                "state": pull.state,
                "base_sha": pull.base.sha,
                "head_sha": pull.head.sha,
                "changed_files": changed_files,
            }

        return self._execute(fetch)