# -*- coding: utf-8 -*-
"""Codex 开关必须同时约束自动授权和手动补跑入口。"""

from config import codex as codex_cfg
from core import codex_retry_service, db, registration_service
from webui.app import create_app


def _account():
    return {
        "id": 7,
        "email": "codex-toggle@example.test",
        "codex_status": "failed",
        "live_check_status": "live",
    }


def _failed_job():
    return {
        "id": 48,
        "status": "failed",
        "account_id": 7,
        "email": "codex-toggle@example.test",
        "email_source": "generic_api",
    }


def _patch_retry_lookup(monkeypatch):
    account = _account()
    monkeypatch.setattr(registration_service.db, "get_successful_retry_for_job", lambda _job_id: None)
    monkeypatch.setattr(registration_service.db, "get_account", lambda _account_id: dict(account))
    monkeypatch.setattr(registration_service.db, "get_account_by_email", lambda _email: dict(account))


def test_disabled_codex_switch_removes_manual_job_retry(monkeypatch):
    _patch_retry_lookup(monkeypatch)
    monkeypatch.setattr(codex_cfg, "ENABLE_CODEX_AUTO", False)

    info = registration_service.get_retry_info(_failed_job())

    assert info["retryable"] is False
    assert info["retry_action"] is None
    assert "Codex" in info["retry_reason"]
    assert "关闭" in info["retry_reason"]


def test_codex_switch_accepts_string_false_from_hot_reload(monkeypatch):
    monkeypatch.setattr(codex_cfg, "ENABLE_CODEX_AUTO", "False")

    assert registration_service.codex_authorization_enabled() is False


def test_enabled_codex_switch_keeps_manual_job_retry(monkeypatch):
    _patch_retry_lookup(monkeypatch)
    monkeypatch.setattr(codex_cfg, "ENABLE_CODEX_AUTO", True)

    info = registration_service.get_retry_info(_failed_job())

    assert info["retryable"] is True
    assert info["retry_action"] == "codex"


def test_codex_retry_endpoint_returns_conflict_when_disabled(monkeypatch):
    account = _account()
    monkeypatch.setattr(codex_cfg, "ENABLE_CODEX_AUTO", False)
    monkeypatch.setattr(db, "get_account_by_email", lambda _email: dict(account))

    client = create_app(auth_code="test-auth").test_client()
    response = client.post(
        "/api/codex/retry",
        headers={"X-Auth-Code": "test-auth"},
        json={"email": account["email"]},
    )

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["ok"] is False
    assert "关闭" in payload["error"]


def test_accounts_page_exposes_codex_switch_for_ui(monkeypatch):
    monkeypatch.setattr(codex_cfg, "ENABLE_CODEX_AUTO", False)
    monkeypatch.setattr(db, "list_accounts_page", lambda **_kwargs: {"items": [], "total": 0})

    client = create_app(auth_code="test-auth").test_client()
    response = client.get(
        "/api/accounts?paged=1&page=1&page_size=20",
        headers={"X-Auth-Code": "test-auth"},
    )

    assert response.status_code == 200
    assert response.get_json()["codex_authorization_enabled"] is False


def test_codex_retry_bulk_endpoint_returns_conflict_when_disabled(monkeypatch):
    account = _account()
    monkeypatch.setattr(codex_cfg, "ENABLE_CODEX_AUTO", False)
    monkeypatch.setattr(db, "get_account", lambda _account_id: dict(account))

    client = create_app(auth_code="test-auth").test_client()
    response = client.post(
        "/api/codex/retry-bulk",
        headers={"X-Auth-Code": "test-auth"},
        json={"account_ids": [account["id"]]},
    )

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["ok"] is False
    assert "关闭" in payload["error"]


def test_reserved_worker_rechecks_switch_before_oauth(monkeypatch):
    updates = []
    monkeypatch.setattr(codex_cfg, "ENABLE_CODEX_AUTO", False)
    monkeypatch.setattr(
        codex_retry_service.db,
        "update_account_codex_status",
        lambda email, status, error=None: updates.append((email, status, error)) or True,
    )

    result = codex_retry_service.run_worker("race@example.test")

    assert result["status"] == "skipped"
    assert result["ok"] is False
    assert updates == [("race@example.test", "skipped", result["message"])]
