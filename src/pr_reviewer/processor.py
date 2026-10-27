
from typing import Any

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

        if job["status"] == "failed":
            raise RuntimeError(
                f"Job {delivery_id} has permanently failed; "
                "manual recovery is required"
            )

        claim_token = self.store.claim_job_with_token(delivery_id)

        if claim_token is None:
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
                claim_token=claim_token,
            )

            if not saved:
                raise RuntimeError(
                    f"Could not persist PR data for job {delivery_id}"
                )

            completed = self.store.update_status(
                delivery_id,
                "completed",
                claim_token=claim_token,
            )
            if not completed:
                raise RuntimeError(
                    f"Claim for job {delivery_id} expired before completion"
                )

            result = self.store.get_job(delivery_id)
            if result is None:
                raise RuntimeError(
                    f"Job {delivery_id} disappeared after processing"
                )

            return result

        except GitHubClientError as exc:
            # The GitHub client has already exhausted its bounded API retries.
            status = "retryable" if exc.retryable else "failed"
            self.store.update_status(
                delivery_id,
                status,
                error=str(exc),
                retry_after_seconds=exc.retry_after_seconds,
                claim_token=claim_token,
            )
            raise

        except Exception as exc:
            self.store.update_status(
                delivery_id,
                "failed",
                error=str(exc),
                claim_token=claim_token,
            )
            raise
