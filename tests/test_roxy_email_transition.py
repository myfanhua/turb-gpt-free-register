# -*- coding: utf-8 -*-

import pytest

from core import roxy_registration as registration


class _Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(float(seconds), 0.01)


class _Driver:
    current_url = "https://chatgpt.com/auth/login?email=test%40example.com"


def test_loading_email_transition_does_not_submit_the_form_twice(monkeypatch):
    """Catches the duplicate-submit loop while login?email is still settling."""
    clock = _Clock()
    recovery_calls = []
    driver = _Driver()

    monkeypatch.setattr(registration.time, "time", clock.time)
    monkeypatch.setattr(registration.time, "sleep", clock.sleep)
    monkeypatch.setattr(registration, "_has_access_token", lambda _driver: False)
    monkeypatch.setattr(registration, "_is_login_password_page", lambda _driver: False)
    monkeypatch.setattr(registration, "_is_email_verification_page", lambda _driver: False)
    monkeypatch.setattr(registration, "_is_signup_password_page", lambda _driver: False)
    monkeypatch.setattr(
        registration,
        "_email_input_value_state",
        lambda _driver: {
            "url": "https://chatgpt.com/auth/login?email=test%40example.com",
            "inputs": [{"type": "email", "name": "email", "value": ""}],
        },
    )
    monkeypatch.setattr(registration, "_is_email_login_page_still_present", lambda _driver: True)
    monkeypatch.setattr(
        registration,
        "_recover_email_submit_if_stuck",
        lambda _driver, _email: recovery_calls.append(_email) or {"ok": True},
    )

    state = registration._wait_email_submit_next_state(
        driver,
        "test@example.com",
        timeout=4,
    )

    assert state == "email_page"
    assert recovery_calls == []


def test_stuck_email_page_uses_nextauth_fallback_before_retyping(monkeypatch):
    """Catches losing the auth redirect when the rendered login form stalls."""
    flow = {"fallback_started": False, "type_calls": 0}
    driver = _Driver()

    def type_email(_driver, _email, timeout=20):
        flow["type_calls"] += 1

    def wait_next(_driver, _email, timeout=20):
        return "otp" if flow["fallback_started"] else "email_page"

    def start_fallback(_driver, _email):
        flow["fallback_started"] = True
        return {"ok": True, "stage": "redirect", "url": "https://auth.openai.com/authorize"}

    monkeypatch.setattr(registration, "_type_email_address", type_email)
    monkeypatch.setattr(
        registration,
        "_email_input_value_state",
        lambda _driver: {"inputs": [{"value": "test@example.com"}]},
    )
    monkeypatch.setattr(registration, "_submit_email_step", lambda _driver, _email: None)
    monkeypatch.setattr(registration, "_wait_email_submit_next_state", wait_next)
    monkeypatch.setattr(registration, "_submit_email_via_browser_nextauth", start_fallback)
    monkeypatch.setattr(registration, "human_delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(registration.time, "sleep", lambda _seconds: None)

    result = registration._submit_email_and_wait_next(
        driver,
        "test@example.com",
        attempts=3,
    )

    assert result == "otp"
    assert flow == {"fallback_started": True, "type_calls": 1}


def test_password_timeout_does_not_fall_through_to_otp(monkeypatch):
    """Catches the misleading 'missing OTP input' after password never submitted."""
    clock = _Clock()

    class PasswordDriver(_Driver):
        def __init__(self):
            self.script_calls = 0

        def execute_script(self, _script):
            self.script_calls += 1
            if self.script_calls == 1:
                return {"ok": True, "input": object(), "button": object()}
            return {
                "ok": True,
                "reason": "enabled_submit_target",
                "button": object(),
                "text": "Continue",
                "type": "submit",
                "dd": "Continue",
                "ariaDisabled": "false",
            }

    driver = PasswordDriver()
    password_state = {
        "url": "https://auth.openai.com/create-account/password",
        "inputs": [{"type": "password", "visible": True, "value": "<password>"}],
        "buttons": [{"type": "submit", "disabled": False, "visible": True}],
    }

    monkeypatch.setattr(registration.time, "time", clock.time)
    monkeypatch.setattr(registration.time, "sleep", clock.sleep)
    monkeypatch.setattr(registration, "human_delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(registration, "_is_email_verification_page", lambda _driver: False)
    monkeypatch.setattr(registration, "_has_access_token", lambda _driver: False)
    monkeypatch.setattr(registration, "_password_page_state", lambda _driver: password_state)
    monkeypatch.setattr(registration, "_is_signup_password_page", lambda _driver: True)
    monkeypatch.setattr(registration, "_is_login_password_page", lambda _driver: False)
    monkeypatch.setattr(registration, "_registration_password", lambda: "FixedPass123!x")
    monkeypatch.setattr(registration, "_human_type_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(registration, "_human_click", lambda *args, **kwargs: None)
    monkeypatch.setattr(registration, "_click_continue", lambda _driver: None)
    monkeypatch.setattr(
        registration,
        "_click_passwordless_signup_if_present",
        lambda _driver: {"ok": False, "reason": "missing_passwordless_button"},
    )

    with pytest.raises(RuntimeError, match="密码提交后仍停留在密码页"):
        registration._fill_password_page_if_present(
            driver,
            "test@example.com",
            timeout=5,
        )


def test_signup_password_failure_falls_back_to_one_time_code(monkeypatch):
    """Catches the recoverable signup-password failure before OTP."""
    clock = _Clock()
    flow = {"passwordless_clicked": False}

    class PasswordDriver(_Driver):
        def __init__(self):
            self.script_calls = 0

        def execute_script(self, _script):
            self.script_calls += 1
            if self.script_calls == 1:
                return {"ok": True, "input": object(), "button": object()}
            return {
                "ok": True,
                "reason": "enabled_submit_target",
                "button": object(),
                "text": "Continue",
                "type": "submit",
                "dd": "Continue",
                "ariaDisabled": "false",
            }

    driver = PasswordDriver()
    password_state = {
        "url": "https://auth.openai.com/create-account/password",
        "inputs": [{"type": "password", "visible": True, "value": "<password>"}],
        "buttons": [{"type": "submit", "disabled": False, "visible": True}],
    }

    def click_passwordless(_driver):
        flow["passwordless_clicked"] = True
        return {"ok": True, "reason": "clicked_passwordless_send_otp"}

    monkeypatch.setattr(registration.time, "time", clock.time)
    monkeypatch.setattr(registration.time, "sleep", clock.sleep)
    monkeypatch.setattr(registration, "human_delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        registration,
        "_is_email_verification_page",
        lambda _driver: flow["passwordless_clicked"],
    )
    monkeypatch.setattr(registration, "_has_access_token", lambda _driver: False)
    monkeypatch.setattr(registration, "_password_page_state", lambda _driver: password_state)
    monkeypatch.setattr(registration, "_is_signup_password_page", lambda _driver: not flow["passwordless_clicked"])
    monkeypatch.setattr(registration, "_is_login_password_page", lambda _driver: False)
    monkeypatch.setattr(registration, "_registration_password", lambda: "FixedPass123!x")
    monkeypatch.setattr(registration, "_human_type_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(registration, "_human_click", lambda *args, **kwargs: None)
    monkeypatch.setattr(registration, "_click_continue", lambda _driver: None)
    monkeypatch.setattr(registration, "_click_passwordless_signup_if_present", click_passwordless)

    password = registration._fill_password_page_if_present(
        driver,
        "test@example.com",
        timeout=5,
    )

    assert password is None
    assert flow["passwordless_clicked"] is True


def test_existing_otp_page_is_kept_on_one_time_code(monkeypatch):
    """The OTP page must not be diverted into the failing password branch."""
    clock = _Clock()
    clicked = []
    driver = _Driver()

    monkeypatch.setattr(registration.time, "time", clock.time)
    monkeypatch.setattr(registration.time, "sleep", clock.sleep)
    monkeypatch.setattr(registration, "_is_email_verification_page", lambda _driver: True)
    monkeypatch.setattr(registration, "_has_access_token", lambda _driver: False)
    monkeypatch.setattr(
        registration,
        "_click_continue_with_password_if_present",
        lambda _driver: clicked.append(True) or {"ok": True},
    )

    password = registration._fill_password_page_if_present(
        driver,
        "test@example.com",
        timeout=3,
    )

    assert password is None
    assert clicked == []


def test_signup_password_prefers_one_time_code_before_setting_password(monkeypatch):
    """Signup password pages should use their passwordless OTP form first."""
    clock = _Clock()
    flow = {"passwordless_clicked": False}
    driver = _Driver()

    def click_passwordless(_driver):
        flow["passwordless_clicked"] = True
        return {"ok": True, "reason": "clicked_passwordless_send_otp"}

    monkeypatch.setattr(registration.time, "time", clock.time)
    monkeypatch.setattr(registration.time, "sleep", clock.sleep)
    monkeypatch.setattr(
        registration,
        "_is_email_verification_page",
        lambda _driver: flow["passwordless_clicked"],
    )
    monkeypatch.setattr(registration, "_has_access_token", lambda _driver: False)
    monkeypatch.setattr(registration, "_password_page_state", lambda _driver: {"url": "/create-account/password"})
    monkeypatch.setattr(registration, "_is_signup_password_page", lambda _driver: not flow["passwordless_clicked"])
    monkeypatch.setattr(registration, "_is_login_password_page", lambda _driver: False)
    monkeypatch.setattr(registration, "_click_passwordless_signup_if_present", click_passwordless)
    monkeypatch.setattr(
        registration,
        "_registration_password",
        lambda: pytest.fail("password generation must not run before the OTP fallback"),
    )

    password = registration._fill_password_page_if_present(
        driver,
        "test@example.com",
        timeout=3,
    )

    assert password is None
    assert flow["passwordless_clicked"] is True


def test_passwordless_helper_submits_structural_form_when_control_is_missing(monkeypatch):
    """A partially rendered localized page still exposes a dedicated OTP form."""

    class StructuralFormDriver(_Driver):
        def execute_script(self, script):
            if "structural_passwordless_form" in script:
                return {"ok": True, "reason": "submitted_structural_passwordless_form"}
            return {"ok": False, "reason": "missing_passwordless_button"}

    result = registration._click_passwordless_signup_if_present(StructuralFormDriver())

    assert result == {"ok": True, "reason": "submitted_structural_passwordless_form"}


def test_otp_timeout_still_on_otp_page_is_invalid_not_accepted(monkeypatch):
    """Remaining on the OTP form is not progress and must not enter profile wait."""
    clock = _Clock()
    driver = _Driver()

    monkeypatch.setattr(registration.time, "time", clock.time)
    monkeypatch.setattr(registration.time, "sleep", clock.sleep)
    monkeypatch.setattr(registration, "_is_email_verification_page", lambda _driver: True)
    monkeypatch.setattr(
        registration,
        "_email_otp_page_state",
        lambda _driver: {
            "url": "https://auth.openai.com/email-verification",
            "inputs": [{"name": "code", "ariaInvalid": ""}],
            "errors": [],
        },
    )

    assert registration._wait_after_email_otp_submit(driver, timeout=2) == "invalid"


def test_fresh_otp_wait_skips_rejected_provider_value(monkeypatch):
    """A resend must wait for a changed OTP instead of resubmitting the old one."""
    clock = _Clock()
    returned = iter(["111111", "111111", "222222"])
    calls = []

    def fake_wait(email, after_ts, **kwargs):
        calls.append((email, after_ts, kwargs))
        return next(returned)

    monkeypatch.setattr(registration.time, "time", clock.time)
    monkeypatch.setattr(registration.time, "sleep", clock.sleep)
    monkeypatch.setattr(registration, "wait_for_otp", fake_wait)

    code = registration._wait_for_fresh_email_otp(
        "test@example.com",
        after_ts=10.0,
        rejected_codes={"111111"},
        max_wait=20,
        poll_interval=1,
    )

    assert code == "222222"
    assert len(calls) == 3


def test_terminal_otp_failures_quarantine_mailbox_instead_of_recycling():
    """Mailbox/API pairs that cannot provide a fresh OTP must leave the queue."""
    assert registration._registration_failure_email_status(
        RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数"),
        create_acknowledged=False,
    ) == "failed"
    assert registration._registration_failure_email_status(
        RuntimeError("找不到 OTP 输入框"),
        create_acknowledged=False,
    ) == "failed"
    assert registration._registration_failure_email_status(
        RuntimeError("登录页暂时加载失败"),
        create_acknowledged=False,
    ) == "available"


def test_wait_for_otp_input_allows_retry_page_to_restore_form(monkeypatch):
    """After Retry/Resend, do not type until the OTP field is actually back."""
    clock = _Clock()
    states = iter([
        {"url": "https://auth.openai.com/email-verification", "inputs": []},
        {"url": "https://auth.openai.com/email-verification", "inputs": []},
        {
            "url": "https://auth.openai.com/email-verification",
            "inputs": [{"name": "code", "autocomplete": "one-time-code", "inputmode": "numeric"}],
        },
    ])
    last = {"url": "https://auth.openai.com/email-verification", "inputs": []}

    def state(_driver):
        nonlocal last
        try:
            last = next(states)
        except StopIteration:
            pass
        return last

    monkeypatch.setattr(registration.time, "time", clock.time)
    monkeypatch.setattr(registration.time, "sleep", clock.sleep)
    monkeypatch.setattr(registration, "_email_otp_page_state", state)

    recovered = registration._wait_for_email_otp_input(_Driver(), timeout=5)

    assert recovered["inputs"][0]["name"] == "code"
