import time
from typing import Any, Callable

import requests
from github import Github, GithubException

from pr_reviewer.config import Settings


class GitHubClientError(RuntimeError):
    """Raised when GitHub PR data cannot be retrieved."""


class GitHubPRClient:
    """Retrieve GitHub pull-request metadata and changed files."""

    RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        settings: Settings,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        github_factory: Callable[..., Any] = Github,
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
                )

            return False

        if isinstance(exc, requests.RequestException):
            return True

        return False

    def _execute(self, operation: Callable[[], Any]) -> Any:
        """Run an API operation with a bounded exponential-backoff retry."""
        for attempt in range(1, self.max_attempts + 1):
            try:
                return operation()
            except Exception as exc:
                if (
                    attempt >= self.max_attempts
                    or not self._is_retryable(exc)
                ):
                    raise GitHubClientError(
                        f"GitHub API operation failed after "
                        f"{attempt} attempt(s): {exc}"
                    ) from exc

                delay = self.base_delay * (2 ** (attempt - 1))
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