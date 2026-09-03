# -*- coding: utf-8 -*-
import json


def _point_db_at_tmp(monkeypatch, tmp_path):
    from core import db

    paths = {
        "_GENERIC_API_EMAIL_JSON": tmp_path / "generic_api.json",
        "_GENERIC_API_EMAIL_TXT": tmp_path / "generic_api.txt",
        "_OUTLOOK_JSON": tmp_path / "outlook.json",
        "_OUTLOOK_TXT": tmp_path / "outlook.txt",
        "_ACCOUNTS_JSON": tmp_path / "accounts.json",
        "_ACCOUNTS_TXT": tmp_path / "accounts.txt",
        "_TOKENS_TXT": tmp_path / "tokens.txt",
        "_JOBS_JSON": tmp_path / "jobs.json",
        "_LOG_DIR": tmp_path / "logs",
        "_CODEX_DIR": tmp_path / "codex_accounts",
        "_CODEX_EXPORT_STATE": tmp_path / "codex_export.json",
    }
    for name, path in paths.items():
        monkeypatch.setattr(db, name, path)
    return db, paths


def test_generic_api_import_rejects_non_email_label(monkeypatch, tmp_path):
    db, _paths = _point_db_at_tmp(monkeypatch, tmp_path)

    inserted, skipped = db.import_generic_api_emails([
        {"email": "发货形式为邮箱", "code_url": "https://mail.example/invalid"},
        {"email": "valid@example.com", "code_url": "https://mail.example/valid"},
    ])

    assert (inserted, skipped) == (1, 1)
    assert [row["email"] for row in db.list_generic_api_email_pool()] == ["valid@example.com"]


def test_generic_api_claim_quarantines_preexisting_invalid_available_row(monkeypatch, tmp_path):
    db, paths = _point_db_at_tmp(monkeypatch, tmp_path)
    paths["_GENERIC_API_EMAIL_JSON"].write_text(json.dumps([
        {
            "id": 1,
            "email": "发货形式为邮箱",
            "code_url": "https://mail.example/invalid",
            "status": "available",
        },
        {
            "id": 2,
            "email": "valid@example.com",
            "code_url": "https://mail.example/valid",
            "status": "available",
        },
    ], ensure_ascii=False), encoding="utf-8")

    claimed = db.claim_next_generic_api_email()
    summary = db.generic_api_email_pool_summary()
    persisted = json.loads(paths["_GENERIC_API_EMAIL_JSON"].read_text(encoding="utf-8"))

    assert claimed["email"] == "valid@example.com"
    assert summary == {"available": 0, "used": 1, "failed": 0, "disabled": 1, "invalid": 1, "total": 2}
    invalid = next(row for row in persisted if row["id"] == 1)
    assert invalid["status"] == "disabled"
    assert "邮箱格式无效" in invalid["note"]


def test_generic_api_claim_skips_and_reconciles_already_registered_mailbox(monkeypatch, tmp_path):
    """A stale available flag must never recycle an already-created account."""
    db, paths = _point_db_at_tmp(monkeypatch, tmp_path)
    registered_email = "registered@example.com"
    fresh_email = "fresh@example.com"
    paths["_ACCOUNTS_JSON"].write_text(json.dumps([
        {"id": 91, "email": registered_email, "access_token": "token"},
    ]), encoding="utf-8")
    paths["_GENERIC_API_EMAIL_JSON"].write_text(json.dumps([
        {
            "id": 1,
            "email": registered_email,
            "code_url": "https://mail.example/registered",
            "status": "available",
            "used_at": None,
        },
        {
            "id": 2,
            "email": fresh_email,
            "code_url": "https://mail.example/fresh",
            "status": "available",
            "used_at": None,
        },
    ]), encoding="utf-8")

    claimed = db.claim_next_generic_api_email()
    persisted = json.loads(paths["_GENERIC_API_EMAIL_JSON"].read_text(encoding="utf-8"))
    registered = next(row for row in persisted if row["id"] == 1)

    assert claimed["email"] == fresh_email
    assert registered["status"] == "used"
    assert registered["registered_account_id"] == 91
    assert "已存在注册账号" in registered["note"]


def test_create_app_recovers_interrupted_registration_and_releases_email(monkeypatch, tmp_path):
    db, paths = _point_db_at_tmp(monkeypatch, tmp_path)
    email = "interrupted@example.com"
    paths["_GENERIC_API_EMAIL_JSON"].write_text(json.dumps([
        {
            "id": 1,
            "email": email,
            "code_url": "https://mail.example/otp",
            "status": "used",
            "used_at": "2026-08-30T17:28:45",
        }
    ]), encoding="utf-8")
    log_path = paths["_LOG_DIR"] / "job-51.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("17:30:04 [INFO] waiting for OTP\n", encoding="utf-8")
    paths["_JOBS_JSON"].write_text(json.dumps([
        {
            "id": 51,
            "job_uuid": "job-51",
            "job_type": "registration",
            "email_source": "generic_api",
            "email": email,
            "status": "running",
            "error_message": None,
            "log_file": str(log_path),
            "started_at": "2026-08-30T17:28:45",
            "completed_at": None,
            "account_id": None,
            "created_at": "2026-08-30T17:28:45",
        }
    ]), encoding="utf-8")

    from webui.app import create_app
    create_app(auth_code="test-auth")

    recovered = db.get_job(51)
    pool_row = db.get_generic_api_email_by_email(email)
    assert recovered["status"] == "stopped"
    assert recovered["completed_at"]
    assert recovered["error_message"] == "服务重启，任务已中断"
    assert pool_row["status"] == "available"
    assert "服务重启" in log_path.read_text(encoding="utf-8")
