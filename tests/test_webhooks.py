import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from pr_reviewer.config import Settings
from pr_reviewer.jobs import JobStore
from pr_reviewer.main import create_app


SECRET = "test-webhook-secret"


@pytest.fixture
def setup_client(tmp_path):
    db_path = tmp_path / "review_jobs.sqlite3"
    store = JobStore(db_path)
    settings = Settings(
        _env_file=None,
        app_env="test",
        github_webhook_secret=SecretStr(SECRET),
    )
    app = create_app(settings=settings, store=store)
    return TestClient(app), store, db_path


def payload(action="opened"):
    return {
        "action": action,
        "number": 42,
        "repository": {"full_name": "example/project"},
        "pull_request": {"number": 42},
    }


def encoded_payload(data):
    return json.dumps(data, separators=(",", ":")).encode()


def signed_headers(body, delivery="delivery-123", event="pull_request"):
    digest = hmac.new(
        SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": f"sha256={digest}",
        "Content-Type": "application/json",
    }


def test_valid_signed_pull_request_creates_job(setup_client):
    client, store, _ = setup_client
    body = encoded_payload(payload())

    response = client.post(
        "/webhooks/github",
        content=body,
        headers=signed_headers(body),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert store.count_jobs() == 1

    job = store.get_job("delivery-123")
    assert job is not None
    assert job["repository"] == "example/project"
    assert job["pull_request_number"] == 42
    assert job["action"] == "opened"
    assert job["status"] == "pending"


@pytest.mark.parametrize("signature", [None, "sha256=invalid"])
def test_missing_or_invalid_signature_is_rejected(setup_client, signature):
    client, store, _ = setup_client
    body = encoded_payload(payload())
    headers = signed_headers(body)

    if signature is None:
        headers.pop("X-Hub-Signature-256")
    else:
        headers["X-Hub-Signature-256"] = signature

    response = client.post(
        "/webhooks/github",
        content=body,
        headers=headers,
    )

    assert response.status_code == 401
    assert store.count_jobs() == 0


def test_unsupported_event_does_not_create_job(setup_client):
    client, store, _ = setup_client
    body = encoded_payload(payload())

    response = client.post(
        "/webhooks/github",
        content=body,
        headers=signed_headers(body, event="issues"),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "ignored"
    assert store.count_jobs() == 0


def test_unsupported_action_does_not_create_job(setup_client):
    client, store, _ = setup_client
    body = encoded_payload(payload(action="closed"))

    response = client.post(
        "/webhooks/github",
        content=body,
        headers=signed_headers(body),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "ignored"
    assert store.count_jobs() == 0


def test_duplicate_delivery_does_not_create_second_job(setup_client):
    client, store, _ = setup_client
    body = encoded_payload(payload())
    headers = signed_headers(body)

    first = client.post("/webhooks/github", content=body, headers=headers)
    second = client.post("/webhooks/github", content=body, headers=headers)

    assert first.status_code == 202
    assert first.json()["status"] == "accepted"
    assert second.status_code == 202
    assert second.json()["status"] == "duplicate"
    assert store.count_jobs() == 1


def test_missing_delivery_id_is_rejected(setup_client):
    client, store, _ = setup_client
    body = encoded_payload(payload())
    headers = signed_headers(body)
    headers.pop("X-GitHub-Delivery")

    response = client.post(
        "/webhooks/github",
        content=body,
        headers=headers,
    )

    assert response.status_code == 400
    assert store.count_jobs() == 0


def test_job_survives_store_recreation(setup_client):
    client, store, db_path = setup_client
    body = encoded_payload(payload())

    response = client.post(
        "/webhooks/github",
        content=body,
        headers=signed_headers(body),
    )

    assert response.status_code == 202

    reopened_store = JobStore(db_path)
    assert reopened_store.count_jobs() == 1
    assert reopened_store.get_job("delivery-123") is not None


def test_missing_secret_fails_closed(tmp_path):
    settings = Settings(_env_file=None, app_env="test")
    store = JobStore(tmp_path / "no_secret.sqlite3")
    client = TestClient(create_app(settings=settings, store=store))
    body = encoded_payload(payload())

    response = client.post(
        "/webhooks/github",
        content=body,
        headers=signed_headers(body),
    )

    assert response.status_code == 503
    assert store.count_jobs() == 0


def test_malformed_json_is_rejected(setup_client):
    client, store, _ = setup_client
    body = b"{invalid json"

    response = client.post(
        "/webhooks/github",
        content=body,
        headers=signed_headers(body),
    )

    assert response.status_code == 400
    assert store.count_jobs() == 0
