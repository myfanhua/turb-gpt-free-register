# -*- coding: utf-8 -*-
import json
from pathlib import Path

from core import db
from webui.app import _compact_account_for_list, create_app


def _use_tmp_accounts(monkeypatch, tmp_path, rows):
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(db, "_ACCOUNTS_JSON", accounts)
    monkeypatch.setattr(db, "_LEGACY_ACCOUNTS_JSON", tmp_path / "legacy.json")
    monkeypatch.setattr(db, "_GENERIC_API_EMAIL_JSON", tmp_path / "generic.json")
    (tmp_path / "generic.json").write_text("[]", encoding="utf-8")


def test_account_category_priority_and_labels():
    assert db.account_category({"live_check_status": "deactivated", "plan_type": "free", "plus_trial_eligible": True}) == "deactivated"
    assert db.account_category({"plan_type": "free", "plus_trial_eligible": True}) == "trial_eligible"
    assert db.account_category({"plan_type": "plus", "plus_trial_eligible": False}) == "plus"
    assert db.account_category({"plan_type": "free", "plus_trial_eligible": False}) == "free"
    assert db.account_category({}) == "unverified"
    assert db.ACCOUNT_CATEGORY_LABELS["deactivated"] == "已废"
    assert db.ACCOUNT_CATEGORY_LABELS["trial_eligible"] == "有试用资格"


def test_account_category_filter_is_applied_to_page(monkeypatch, tmp_path):
    _use_tmp_accounts(
        monkeypatch,
        tmp_path,
        [
            {"id": 1, "email": "dead@example.test", "plan_type": "free", "live_check_status": "deactivated"},
            {"id": 2, "email": "trial@example.test", "plan_type": "free", "plus_trial_eligible": True},
            {"id": 3, "email": "free@example.test", "plan_type": "free", "plus_trial_eligible": False},
        ],
    )
    result = db.list_accounts_page(limit=20, category_filter="deactivated")
    assert result["total"] == 1
    assert result["items"][0]["email"] == "dead@example.test"
    assert result["items"][0]["account_category"] == "deactivated"


def test_compact_account_exposes_category_and_label():
    compact = _compact_account_for_list(
        {"id": 8, "email": "trial@example.test", "plan_type": "free", "plus_trial_eligible": True}
    )
    assert compact["account_category"] == "trial_eligible"
    assert compact["account_category_label"] == "有试用资格"


def test_completed_rebind_endpoint_returns_separate_email_output(monkeypatch):
    completed = [
        {
            "id": 8,
            "old_email": "old@example.test",
            "new_email": "new@example.test",
            "completed_at": "2026-08-31T10:00:00",
            "rebind_status": "success",
            "access_token": "must-not-leak",
            "password": "must-not-leak",
        }
    ]
    monkeypatch.setattr(db, "list_rebind_completed", lambda **kwargs: {"items": completed, "total": 1})
    client = create_app(auth_code="test-auth").test_client()
    response = client.get("/api/rebind/completed", headers={"X-Auth-Code": "test-auth"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 1
    assert payload["text"] == "new@example.test"
    assert payload["items"][0]["new_email"] == "new@example.test"
    assert "access_token" not in payload["items"][0]
    assert "password" not in payload["items"][0]


def test_console_exposes_category_filter_and_completed_rebind_output():
    client = create_app(auth_code="test-auth").test_client()
    response = client.get("/", headers={"X-Auth-Code": "test-auth", "Accept-Encoding": "identity"})
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="accountCategoryFilterV2"' in html
    assert "有试用资格" in html
    assert "已废" in html
    assert 'id="rebindCompletedOutputV2"' in html
    assert "function loadRebindCompletedV2()" in html
    assert "/api/rebind/completed" in html
