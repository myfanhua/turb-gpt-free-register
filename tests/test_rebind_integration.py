import json
from pathlib import Path
from types import SimpleNamespace

from core import db, rebind_service


def _use_tmp_storage(monkeypatch, tmp_path):
    paths = {
        "_ACCOUNTS_JSON": tmp_path / "accounts.json",
        "_ACCOUNTS_TXT": tmp_path / "accounts.txt",
        "_TOKENS_TXT": tmp_path / "tokens.txt",
        "_VIEWER_HTML": tmp_path / "viewer.html",
        "_OUTLOOK_JSON": tmp_path / "outlook.json",
        "_OUTLOOK_TXT": tmp_path / "outlook.txt",
        "_GENERIC_API_EMAIL_JSON": tmp_path / "generic.json",
        "_GENERIC_API_EMAIL_TXT": tmp_path / "generic.txt",
        "_JOBS_JSON": tmp_path / "jobs.json",
        "_LOG_DIR": tmp_path / "logs",
    }
    for name, value in paths.items():
        monkeypatch.setattr(db, name, value)
    monkeypatch.setattr(rebind_service, "_LOG_DIR", tmp_path / "logs")


def _account():
    return {
        "id": 7,
        "email": "old@example.com",
        "access_token": "old-access-token",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "original_email_line": "",
        "extra_json": json.dumps({"registration_password": "password-for-test", "user": {"id": "u-old"}}),
        "created_at": "2026-08-30T00:00:00",
    }


def test_success_writes_current_email_token_and_rebind_history(monkeypatch, tmp_path):
    _use_tmp_storage(monkeypatch, tmp_path)
    db._write_json(db._ACCOUNTS_JSON, [_account()])
    bundle_path = tmp_path / "login_bundle.json"
    bundle_path.write_text(
        json.dumps(
            {
                "email": "new@example.com",
                "access_token": "new-access-token",
                "user_id": "u-new",
                "account_id": "acct-new",
                "expires": "2026-09-30T00:00:00",
                "device_id": "device-new",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        rebind_service,
        "run_rebind_email",
        lambda **kwargs: SimpleNamespace(
            ok=True,
            code="OK",
            message="rebind success",
            old_email="old@example.com",
            new_email="new@example.com",
            bundle_path=str(bundle_path),
            run_dir=str(tmp_path / "run"),
            access_token_masked="new-...token",
            session_email="new@example.com",
        ),
    )

    result = rebind_service._run_rebind(
        account_id=7,
        new_email="new@example.com",
        mail_api="https://mail.example.test/latest",
        proxy=None,
        mail_timeout=5,
        trigger="test",
    )

    assert result["ok"] is True
    saved = db.get_account(7)
    assert saved["email"] == "new@example.com"
    assert saved["access_token"] == "new-access-token"
    assert saved["user_id"] == "u-new"
    assert saved["account_id"] == "acct-new"
    assert saved["rebind_previous_email"] == "old@example.com"
    assert saved["rebind_status"] == "success"
    assert saved["original_email_line"] == "new@example.com"
    assert saved["copy_line"].startswith("new@example.com----new-access-token")
    extra = json.loads(saved["extra_json"])
    assert extra["registration_password"] == "password-for-test"


def test_failure_preserves_current_credentials_and_records_error(monkeypatch, tmp_path):
    _use_tmp_storage(monkeypatch, tmp_path)
    db._write_json(db._ACCOUNTS_JSON, [_account()])
    monkeypatch.setattr(
        rebind_service,
        "run_rebind_email",
        lambda **kwargs: SimpleNamespace(
            ok=False,
            code="MAIL_TIMEOUT",
            message="验证码超时",
            old_email="old@example.com",
            new_email="new@example.com",
            bundle_path="",
            run_dir=str(tmp_path / "run"),
            access_token_masked="",
            session_email="",
        ),
    )

    result = rebind_service._run_rebind(
        account_id=7,
        new_email="new@example.com",
        mail_api="https://mail.example.test/latest",
        proxy=None,
        mail_timeout=5,
        trigger="test",
    )

    assert result["ok"] is False
    saved = db.get_account(7)
    assert saved["email"] == "old@example.com"
    assert saved["access_token"] == "old-access-token"
    assert saved["rebind_status"] == "failed"
    assert saved["rebind_error"] == "验证码超时"


def test_webui_rebind_route_enqueues_without_returning_mail_api(monkeypatch):
    from webui.app import create_app

    account = _account()
    queued = {}

    monkeypatch.setattr(db, "get_account", lambda account_id: dict(account) if int(account_id) == 7 else None)
    monkeypatch.setattr(db, "get_account_by_email", lambda email: None)

    def fake_enqueue(**kwargs):
        queued.update(kwargs)
        return {
            "accepted": True,
            "busy": False,
            "account_id": 7,
            "old_email": account["email"],
            "new_email": kwargs["new_email"],
            "status": "queued",
        }

    monkeypatch.setattr(rebind_service, "enqueue_account_rebind", fake_enqueue)
    client = create_app(auth_code="test-auth").test_client()
    response = client.post(
        "/api/accounts/7/rebind-email",
        headers={"X-Auth-Code": "test-auth"},
        json={
            "new_email": "new@example.com",
            "mail_api": "https://mail.example.test/latest?token=not-returned",
            "mail_timeout": 30,
        },
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["status"] == "queued"
    assert "mail_api" not in payload
    assert queued["mail_api"].startswith("https://mail.example.test")


def test_rebind_status_route_returns_status_and_log_without_credentials(monkeypatch, tmp_path):
    from webui.app import create_app

    account = _account()
    account.update({
        "rebind_status": "failed",
        "rebind_error": "验证码超时",
        "rebind_message": "验证码超时",
        "rebind_new_email": "new@example.com",
    })
    log_file = tmp_path / "rebind-7.log"
    log_file.write_text("12:00:00 [INFO] [换绑] 失败 [MAIL_TIMEOUT] 验证码超时\n", encoding="utf-8")
    monkeypatch.setattr(db, "get_account", lambda account_id: dict(account) if int(account_id) == 7 else None)
    monkeypatch.setattr(rebind_service, "log_path", lambda account_id: log_file)
    monkeypatch.setattr(rebind_service, "is_running", lambda account_id: False)

    client = create_app(auth_code="test-auth").test_client()
    response = client.get("/api/accounts/7/rebind-email", headers={"X-Auth-Code": "test-auth"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["rebind_status"] == "failed"
    assert payload["rebind_error"] == "验证码超时"
    assert "access_token" not in payload
    assert "password-for-test" not in payload
    assert "验证码超时" in payload["log"]


def test_detect_rebind_resources_prioritizes_configured_domain_provider(monkeypatch):
    from config import email as email_cfg

    values = {
        "EMAIL_SOURCE": "generic_api",
        "CLOUDFLARE_API_BASE": "https://mail.example.test",
        "CLOUDFLARE_API_KEY": "configured-key",
        "CLOUDFLARE_AUTH_MODE": "x-api-key",
        "CLOUDFLARE_DEFAULT_DOMAINS": ["example.test"],
        "EMAIL_DOMAIN": "",
        "QQ_EMAIL": "",
        "QQ_IMAP_PASSWORD": "",
        "CLOUDMAIL_API_BASE": "",
        "CLOUDMAIL_AUTH_TOKEN": "",
        "CLOUDMAIL_ADMIN_EMAIL": "",
        "CLOUDMAIL_PASSWORD": "",
        "CLOUDMAIL_DOMAINS": [],
    }
    for key, value in values.items():
        monkeypatch.setattr(email_cfg, key, value, raising=False)
    monkeypatch.setattr(db, "generic_api_email_pool_summary", lambda: {"available": 9, "total": 9})
    monkeypatch.setattr(db, "outlook_pool_summary", lambda: {"available": 0, "total": 0})
    monkeypatch.setattr(db, "domain_email_pool_summary", lambda: {"available": 0, "total": 0})

    result = rebind_service.detect_rebind_resources()

    assert result["selected"] == "cloudflare"
    cloudflare = next(item for item in result["resources"] if item["source"] == "cloudflare")
    assert cloudflare["ready"] is True
    assert cloudflare["domains"] == ["example.test"]


def test_detect_cloudflare_worker_without_local_domain_list(monkeypatch):
    from config import email as email_cfg

    monkeypatch.setattr(email_cfg, "EMAIL_SOURCE", "generic_api", raising=False)
    monkeypatch.setattr(email_cfg, "CLOUDFLARE_API_BASE", "https://mail.example.test", raising=False)
    monkeypatch.setattr(email_cfg, "CLOUDFLARE_API_KEY", "configured-key", raising=False)
    monkeypatch.setattr(email_cfg, "CLOUDFLARE_AUTH_MODE", "x-api-key", raising=False)
    monkeypatch.setattr(email_cfg, "CLOUDFLARE_DEFAULT_DOMAINS", [], raising=False)
    monkeypatch.setattr(db, "generic_api_email_pool_summary", lambda: {"available": 0, "total": 0})
    monkeypatch.setattr(db, "outlook_pool_summary", lambda: {"available": 0, "total": 0})
    monkeypatch.setattr(db, "domain_email_pool_summary", lambda: {"available": 0, "total": 0})

    result = rebind_service.detect_rebind_resources()

    assert result["selected"] == "cloudflare"
    cloudflare = next(item for item in result["resources"] if item["source"] == "cloudflare")
    assert cloudflare["ready"] is True
    assert "服务端选择域名" in cloudflare["message"]


def test_auto_rebind_mailbox_uses_cloudflare_otp_context_and_releases(monkeypatch):
    from core import cf_temp_mail_client

    fake_account = SimpleNamespace(email="new@example.test")
    fetch_calls = []
    release_calls = []
    monkeypatch.setattr(
        rebind_service,
        "detect_rebind_resources",
        lambda: {
            "selected": "cloudflare",
            "resources": [{"source": "cloudflare", "ready": True}],
        },
    )
    monkeypatch.setattr(cf_temp_mail_client, "pick_account", lambda: fake_account)
    monkeypatch.setattr(
        cf_temp_mail_client,
        "fetch_latest_otp",
        lambda email, **kwargs: fetch_calls.append((email, kwargs)) or "123456",
    )
    monkeypatch.setattr(
        cf_temp_mail_client,
        "release_account",
        lambda email, status="available", note=None: release_calls.append((email, status, note)),
    )

    mailbox = rebind_service.allocate_rebind_mailbox()

    assert mailbox.source == "cloudflare"
    assert mailbox.email == "new@example.test"
    assert mailbox.mail_api == ""
    assert mailbox.fetch_otp(100.0, 30.0) == "123456"
    assert fetch_calls == [("new@example.test", {"after_ts": 100.0, "max_wait": 30, "poll_interval": 3, "settle_seconds": 5})]
    mailbox.release("failed", "test release")
    assert release_calls == [("new@example.test", "failed", "test release")]


def test_webui_auto_rebind_route_does_not_require_manual_mail_inputs(monkeypatch):
    from webui.app import create_app

    account = _account()
    queued = {}
    monkeypatch.setattr(db, "get_account", lambda account_id: dict(account) if int(account_id) == 7 else None)

    def fake_enqueue(**kwargs):
        queued.update(kwargs)
        return {
            "accepted": True,
            "busy": False,
            "account_id": 7,
            "old_email": account["email"],
            "new_email": "new@example.test",
            "status": "queued",
            "provider": "cloudflare",
        }

    monkeypatch.setattr(rebind_service, "enqueue_account_rebind", fake_enqueue)
    client = create_app(auth_code="test-auth").test_client()
    response = client.post(
        "/api/accounts/7/rebind-email",
        headers={"X-Auth-Code": "test-auth"},
        json={"provider": "auto", "mail_timeout": 30},
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["status"] == "queued"
    assert queued["provider"] == "auto"
    assert queued["new_email"] == ""
    assert queued["mail_api"] == ""


def test_rebind_accounts_endpoint_returns_only_trial_eligible_accounts(monkeypatch):
    from webui.app import create_app

    monkeypatch.setattr(
        db,
        "list_account_plan_check_statuses",
        lambda **kwargs: {
            "items": [
                {
                    "id": 7,
                    "email": "trial@example.test",
                    "current_plan_type": "free",
                    "plus_trial_eligible": True,
                    "plus_trial_title": "Plus 试用",
                    "plan_check_status": "success",
                    "plan_checked_at": "2026-08-30T10:00:00",
                    "rebind_status": "failed",
                    "rebind_error": "上次任务失败",
                },
                {
                    "id": 8,
                    "email": "regular@example.test",
                    "current_plan_type": "free",
                    "plus_trial_eligible": False,
                    "plan_check_status": "success",
                },
            ],
            "total": 2,
        },
    )

    client = create_app(auth_code="test-auth").test_client()
    response = client.get("/api/rebind/accounts", headers={"X-Auth-Code": "test-auth"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 1
    assert [item["id"] for item in payload["items"]] == [7]
    assert payload["items"][0]["plus_trial_eligible"] is True
    assert payload["items"][0]["rebind_error"] == "上次任务失败"
    assert "access_token" not in payload["items"][0]
    assert "totp_secret" not in payload["items"][0]


def test_rebind_is_a_separate_sidebar_tab_with_trial_account_list():
    modern = Path(__file__).resolve().parents[1] / "webui/templates/index.html"
    legacy = Path(__file__).resolve().parents[1] / "webui/templates/index_legacy.html"
    modern_text = modern.read_text(encoding="utf-8")
    legacy_text = legacy.read_text(encoding="utf-8")

    assert 'data-tab="rebind"' in modern_text
    assert 'id="tab-rebind"' in modern_text
    assert 'id="rebindTrialAccountsBodyV2"' in modern_text
    outlook_start = modern_text.index('id="tab-outlook"')
    rebind_start = modern_text.index('id="tab-rebind"')
    assert 'id="rebindResourcePanelV2"' not in modern_text[outlook_start:rebind_start]

    assert 'data-tab="rebind"' in legacy_text
    assert 'id="tab-rebind"' in legacy_text
    assert 'id="rebindTrialAccountsBody"' in legacy_text
    outlook_start = legacy_text.index('id="tab-outlook"')
    rebind_start = legacy_text.index('id="tab-rebind"')
    assert 'id="rebindResourcePanel"' not in legacy_text[outlook_start:rebind_start]


def test_rebind_failure_rows_expose_view_reason_action_and_status_log():
    modern = Path(__file__).resolve().parents[1] / "webui/templates/index.html"
    legacy = Path(__file__).resolve().parents[1] / "webui/templates/index_legacy.html"
    modern_text = modern.read_text(encoding="utf-8")
    legacy_text = legacy.read_text(encoding="utf-8")

    assert 'data-trial-rebind-reason' in modern_text
    assert '查看原因' in modern_text
    assert 'rebindReasonPanelV2' in modern_text
    assert 'openRebindReasonV2' in modern_text
    assert '/api/accounts/${encodeURIComponent(accountId)}/rebind-email' in modern_text

    assert 'data-trial-rebind-reason' in legacy_text
    assert '查看原因' in legacy_text
    assert 'rebindReasonPanel' in legacy_text
    assert 'openRebindReason' in legacy_text
    assert "/api/accounts/' + encodeURIComponent(accountId) + '/rebind-email" in legacy_text


def test_pipeline_uses_provider_otp_fetcher_when_no_mail_api(monkeypatch, tmp_path):
    from rebind_core import pipeline

    class FakeLogin:
        access_token = "access-token"
        account_id = "account-id"
        factor_id = "factor-id"
        device_id = "device-id"

    class FakeChangeEmailClient:
        def __init__(self, login, **kwargs):
            self.login = login

        def eligibility(self):
            return {"eligible": True, "eligibility_type": "password"}

        def begin(self, new_email):
            return {"ok": True}

        def verify(self, new_email, code):
            assert code == "123456"
            return {"ok": True}

    calls = []
    bundle = {
        "email": "new@example.test",
        "access_token": "new-access-token",
        "user_id": "user-id",
        "account_id": "account-id",
        "expires": "2026-09-30T00:00:00",
        "device_id": "device-id",
    }
    bundle_path = tmp_path / "bundle.json"

    monkeypatch.setattr(pipeline, "login_with_password_and_totp", lambda *args, **kwargs: FakeLogin())
    monkeypatch.setattr(pipeline, "ChangeEmailClient", FakeChangeEmailClient)
    monkeypatch.setattr(pipeline, "build_login_bundle", lambda login, rebind_email: bundle)

    def fake_write_login_bundle(value, out_dir=None):
        bundle_path.write_text(json.dumps(value), encoding="utf-8")
        return {"bundle": bundle_path}

    monkeypatch.setattr(
        pipeline,
        "write_login_bundle",
        fake_write_login_bundle,
    )

    result = pipeline.run_rebind_email(
        old_email="old@example.test",
        password="password",
        totp_secret="JBSWY3DPEHPK3PXP",
        new_email="new@example.test",
        mail_api="",
        otp_fetcher=lambda after_ts, timeout: calls.append((after_ts, timeout)) or "123456",
        out_dir=tmp_path / "out",
        mail_timeout=30,
    )

    assert result.ok is True
    assert len(calls) == 1
    assert calls[0][1] == 30.0


def test_rebind_controls_are_not_in_account_menu():
    modern = Path(__file__).resolve().parents[1] / "webui/templates/index.html"
    legacy = Path(__file__).resolve().parents[1] / "webui/templates/index_legacy.html"
    modern_text = modern.read_text(encoding="utf-8")
    legacy_text = legacy.read_text(encoding="utf-8")

    assert 'id="rebindResourcePanelV2"' in modern_text
    assert 'id="rebindResourcePanel"' in legacy_text
    assert 'data-account-rebind=' not in modern_text
    assert 'data-account-rebind-log=' not in modern_text
    assert 'data-account-rebind=' not in legacy_text
    assert 'data-account-rebind-log=' not in legacy_text
