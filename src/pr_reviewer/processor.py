
from typing import Any

from github import GithubException

from pr_reviewer.config import Settings
from pr_reviewer.github_client import GitHubClientError, GitHubPRClient
from pr_reviewer.jobs import JobStore


class ReviewJobProcessor:
    """Coordinate persistent job tracking and GitHub PR-data retrieval."""

    def __init__(
        self,
        store: JobStore,
        github_client: GitHubPRClient,
    ):
        self.store = store
        self.github_client = github_client

    def process(self, delivery_id: str) -> dict[str, Any]:
        """Retrieve and persist PR data for a previously recorded job."""
        job = self.store.get_job(delivery_id)

        if job is None:
            raise ValueError(f"Unknown delivery ID: {delivery_id}")

        if job["status"] == "completed":
            return job

        if job["status"] == "processing":
            raise RuntimeError(
            f"Job {delivery_id} is already marked as processing"
            )

        if job["status"] == "failed":
            raise RuntimeError(
                f"Job {delivery_id} has permanently failed; "
                "manual recovery is required"
            )

        claimed = self.store.claim_job(delivery_id)

        if not claimed:
            latest_job = self.store.get_job(delivery_id)

            if latest_job is None:
                raise ValueError(f"Unknown delivery ID: {delivery_id}")

            if latest_job["status"] == "completed":
                return latest_job

            raise RuntimeError(
                f"Could not claim job {delivery_id}; "
                f"current status: {latest_job['status']}"
        )
        try:
            pr_data = self.github_client.get_pull_request(
                job["repository"],
                job["pull_request_number"],
            )

            saved = self.store.save_pull_request_data(
                delivery_id,
                pull_request_url=pr_data["url"],
                head_sha=pr_data["head_sha"],
                title=pr_data["title"],
                changed_files=pr_data["changed_files"],
            )

            if not saved:
                raise RuntimeError(
                    f"Could not persist PR data for job {delivery_id}"
                )

            self.store.update_status(delivery_id, "completed")

            result = self.store.get_job(delivery_id)
            if result is None:
                raise RuntimeError(
                    f"Job {delivery_id} disappeared after processing"
                )

            return result

        except GitHubClientError as exc:
            # The GitHub client has already exhausted its bounded API retries.
            # A permanent API failure is failed; transient failures are
            # reported as retryable by the client-specific exception context.
            status = (
                "failed"
                if self._is_permanent_github_failure(exc)
                else "retryable"
            )
            self.store.update_status(
                delivery_id,
                status,
                error=str(exc),
            )
            raise

        except Exception as exc:
            self.store.update_status(
                delivery_id,
                "failed",
                error=str(exc),
            )
            raise

    @staticmethod
    def _is_permanent_github_failure(exc: Exception) -> bool:
        """Classify known permanent GitHub API failures."""
        cause = exc.__cause__

        if isinstance(cause, GithubException):
            return cause.status in {400, 401, 404, 422}

        return False
