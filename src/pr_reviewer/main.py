import hashlib
import hmac
import json
import os
from fastapi import BackgroundTasks
from pr_reviewer.github_client import GitHubPRClient
from pr_reviewer.processor import ReviewJobProcessor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import SecretStr

from pr_reviewer.config import Settings, get_settings
from pr_reviewer.jobs import JobStore


SUPPORTED_ACTIONS = {"opened", "synchronize"}

_default_db_path = Path(__file__).resolve().parents[2] / "data" / "review_jobs.sqlite3"
job_store = JobStore(os.getenv("JOB_DB_PATH", str(_default_db_path)))


def verify_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """Validate GitHub's SHA-256 webhook signature."""
    if not signature or not signature.startswith("sha256="):
        return False

    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


def create_app(
    settings: Settings | None = None,
    store: JobStore | None = None,
    processor: ReviewJobProcessor | None = None,
    ) -> FastAPI:
    """Create an application with injectable settings and job storage."""
    application = FastAPI(title="Multi-Agent PR Reviewer")
    active_settings = settings or get_settings()
    active_store = store or job_store

    active_processor = processor
    if active_processor is None and active_settings.github_token is not None:
        if active_settings.github_token.get_secret_value().strip():
            active_processor = ReviewJobProcessor(
                store=active_store,
                github_client=GitHubPRClient(settings=active_settings),
            )

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/webhooks/github", status_code=202)
    async def github_webhook(
        background_tasks: BackgroundTasks,
        request: Request,
        x_github_event: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
        x_hub_signature_256: str | None = Header(default=None),
        ) -> dict[str, Any]:
        configured_secret = active_settings.github_webhook_secret

        if configured_secret is None:
            raise HTTPException(
                status_code=503,
                detail="GitHub webhook secret is not configured",
            )

        body = await request.body()
        secret_value = (
            configured_secret.get_secret_value()
            if isinstance(configured_secret, SecretStr)
            else str(configured_secret)
        )

        if not verify_signature(body, x_hub_signature_256, secret_value):
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing webhook signature",
            )

        if x_github_event != "pull_request":
            return {"status": "ignored", "reason": "unsupported_event"}

        if not x_github_delivery or not x_github_delivery.strip():
            raise HTTPException(
                status_code=400,
                detail="Missing X-GitHub-Delivery header",
            )

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400,
                detail="Payload must be a JSON object",
            )

        action = payload.get("action")
        if action not in SUPPORTED_ACTIONS:
            return {"status": "ignored", "reason": "unsupported_action"}

        repository_data = payload.get("repository")
        pull_request_data = payload.get("pull_request")

        if not isinstance(repository_data, dict) or not isinstance(
            pull_request_data, dict
        ):
            raise HTTPException(
                status_code=400,
                detail="Missing repository or pull_request information",
            )

        repository = repository_data.get("full_name")
        number = payload.get("number")

        if not isinstance(repository, str) or not repository.strip():
            raise HTTPException(status_code=400, detail="Invalid repository name")

        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise HTTPException(
                status_code=400,
                detail="Invalid pull request number",
            )

        if pull_request_data.get("number") not in (None, number):
            raise HTTPException(
                status_code=400,
                detail="Pull request number mismatch",
            )

        try:
            created = active_store.record_job(
                delivery_id=x_github_delivery,
                repository=repository,
                pull_request_number=number,
                action=action,
            )
        except Exception:
            raise HTTPException(
                status_code=500,
                detail="Could not record review job",
            )

        if not created:
            return {
                "status": "duplicate",
                "delivery_id": x_github_delivery,
            }

        if active_processor is not None:
            background_tasks.add_task(
                active_processor.process,
                x_github_delivery,
            )

        return {
            "status": "accepted",
            "delivery_id": x_github_delivery,
            "processing_scheduled": active_processor is not None,
        }

    return application


app = create_app()
