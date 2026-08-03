# -*- coding: utf-8 -*-
from decimal import Decimal
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import ANY, Mock, call, patch

import pytest

from core import roxy_codex_oauth
from core.sms_country_router import NoEligibleSmsCountry, PreferredCountrySelector
from core.sms_provider import (
    SmsCountryOffer,
    SmsNoBalanceError,
    SmsNoNumbersError,
    SmsProviderError,
)


def offer(country: str, price: str, count: int = 1) -> SmsCountryOffer:
    return SmsCountryOffer(country, Decimal(price), count)


def cfg(provider="sms_activate", *, switch=2, retries=5):
    return SimpleNamespace(
        SMS_PROVIDER=provider,
        SMS_PREFERRED_COUNTRIES=["A", "B"],
        SMS_COUNTRY="Z",
        SMS_COUNTRY_FAILURE_SWITCH=switch,
        SMS_MAX_PRICE="0.25",
        SMS_MAX_RETRIES=retries,
        SMS_SERVICE="openai",
        SMS_CODE_WAIT=120,
        SMS_POLL_INTERVAL=5,
    )


def phone_flow_patches(*, send_side_effect=None):
    return (
        patch.object(roxy_codex_oauth, "_has_strict_add_phone_form", return_value=True),
        patch.object(roxy_codex_oauth, "_is_phone_code_page", return_value=False),
        patch.object(roxy_codex_oauth, "_ensure_add_phone_input"),
        patch.object(roxy_codex_oauth, "_prepare_phone_submission"),
        patch.object(roxy_codex_oauth, "_click_add_phone_continue_button", return_value={"ok": True}),
        patch.object(roxy_codex_oauth, "_wait_page_settle_after_submit"),
        patch.object(roxy_codex_oauth, "_wait_after_phone_send", side_effect=send_side_effect),
        patch.object(roxy_codex_oauth, "_type_otp"),
        patch.object(roxy_codex_oauth, "_click_if_present", return_value=True),
        patch.object(roxy_codex_oauth, "_wait_after_phone_otp_submit", return_value="accepted"),
        patch.object(roxy_codex_oauth, "_find_any", return_value=object()),
        patch.object(roxy_codex_oauth, "_refresh_add_phone_for_retry"),
        patch.object(roxy_codex_oauth, "_sleep_before_phone_retry"),
        patch.object(roxy_codex_oauth, "human_delay"),
    )


def run_phone_flow(provider_cfg, *, acquire, offers=None, send_side_effect=None, selector=None):
    http = Mock()
    patches = phone_flow_patches(send_side_effect=send_side_effect)
    with ExitStack() as stack:
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "_cfg", provider_cfg))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "_http", return_value=http))
        if selector is not None:
            stack.enter_context(patch.object(roxy_codex_oauth, "_build_sms_country_selector", return_value=selector))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "get_country_offers", return_value=offers or [offer("A", "0.10"), offer("B", "0.20")]))
        if isinstance(acquire, Mock):
            acquire_mock = acquire
            stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "acquire_number", new=acquire_mock))
        else:
            acquire_mock = stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "acquire_number", side_effect=acquire))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "set_status"))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "wait_for_sms_code", return_value="123456"))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "complete"))
        cancel_mock = stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "cancel"))
        for item in patches:
            stack.enter_context(item)
        roxy_codex_oauth._do_phone_verification_if_present(object())
    return acquire_mock, http, cancel_mock


@pytest.mark.parametrize("provider", ["grizzly", "sms_activate", "smsactivate", "hero-sms", "hero_sms"])
def test_build_selector_for_compatible_provider_maps_hot_config(provider):
    provider_cfg = cfg(provider, switch=3)
    provider_cfg.SMS_PREFERRED_COUNTRIES = ["33", "187"]
    provider_cfg.SMS_COUNTRY = "6"
    provider_cfg.SMS_MAX_PRICE = "0.19"

    with patch.object(roxy_codex_oauth.sms_provider, "_cfg", provider_cfg):
        selector = roxy_codex_oauth._build_sms_country_selector()

    assert selector.preferred_countries == ["33", "187"]
    assert selector._failure_switch == 3
    assert selector._max_price == Decimal("0.19")


@pytest.mark.parametrize("provider", ["l", "h", "other"])
def test_build_selector_returns_none_for_fixed_provider(provider):
    with patch.object(roxy_codex_oauth.sms_provider, "_cfg", cfg(provider)):
        assert roxy_codex_oauth._build_sms_country_selector() is None


def test_choose_country_uses_live_offers_and_selector_reason():
    selector = PreferredCountrySelector(["A", "B"], fallback_country="Z")
    http = object()

    with (
        patch.object(roxy_codex_oauth.sms_provider, "_cfg", cfg()),
        patch.object(
            roxy_codex_oauth.sms_provider,
            "get_country_offers",
            return_value=[offer("A", "0.20", 4), offer("B", "0.10", 2)],
        ) as get_offers,
    ):
        country = roxy_codex_oauth._choose_sms_country(selector, http, force=True)

    assert country == "B"
    assert selector.last_reason == "lowest_price"
    get_offers.assert_called_once_with(
        ["A", "B"], service="openai", http=http, force=True
    )


def test_choose_country_uses_saved_order_only_when_price_api_errors():
    selector = PreferredCountrySelector(["A", "B"], fallback_country="Z")
    with patch.object(
        roxy_codex_oauth.sms_provider,
        "get_country_offers",
        side_effect=SmsProviderError("prices unavailable"),
    ):
        assert roxy_codex_oauth._choose_sms_country(selector, object()) == "A"
    assert selector.last_reason == "saved_order_fallback"


def test_choose_country_propagates_no_balance_from_price_api():
    selector = PreferredCountrySelector(["A", "B"], fallback_country="Z")
    with patch.object(
        roxy_codex_oauth.sms_provider,
        "get_country_offers",
        side_effect=SmsNoBalanceError("NO_BALANCE"),
    ):
        with pytest.raises(SmsNoBalanceError):
            roxy_codex_oauth._choose_sms_country(selector, object())


def test_choose_country_propagates_no_eligible_live_result():
    selector = PreferredCountrySelector(["A"], fallback_country="Z")
    with (
        patch.object(
            roxy_codex_oauth.sms_provider,
            "get_country_offers",
            return_value=[offer("A", "0.10", 0)],
        ),
        patch.object(roxy_codex_oauth.logger, "info") as info,
    ):
        with pytest.raises(NoEligibleSmsCountry):
            roxy_codex_oauth._choose_sms_country(selector, object())
    assert any(
        args[2] == "no_eligible_country" and "A=0.10/0" in args[3]
        for args, _kwargs in info.call_args_list
        if len(args) >= 4 and "SMS 国家选择" in str(args[0])
    )


def test_no_balance_stops_after_one_acquire_call():
    acquire = Mock(side_effect=SmsNoBalanceError("NO_BALANCE"))
    with pytest.raises(SmsNoBalanceError):
        run_phone_flow(cfg(), acquire=acquire)
    assert acquire.call_count == 1


def test_no_numbers_switches_country_without_consuming_actual_attempt():
    provider_cfg = cfg(retries=5)
    with patch.object(roxy_codex_oauth.logger, "info") as info:
        acquire, _, _ = run_phone_flow(
            provider_cfg,
            acquire=[SmsNoNumbersError("NO_NUMBERS"), ("id-b", "222")],
        )

    assert acquire.call_args_list == [
        call(ANY, country="A"),
        call(ANY, country="B"),
    ]
    attempt_logs = [args for args, _kwargs in info.call_args_list if "手机验证尝试" in str(args[0])]
    assert attempt_logs == [
        ("[Codex][Browser] 手机验证尝试 %s/%s，provider=%s，号码=+%s", 1, 5, "sms_activate", "222")
    ]


def test_no_numbers_after_activation_is_recorded_as_actual_number_failure():
    selector = PreferredCountrySelector(["A", "B"], fallback_country="Z", failure_switch=1)
    selector.record_number_failure = Mock(wraps=selector.record_number_failure)

    acquire, http, cancel = run_phone_flow(
        cfg(switch=1),
        selector=selector,
        acquire=[("id-a", "111"), ("id-b", "222")],
        send_side_effect=[SmsNoNumbersError("late NO_NUMBERS"), None],
    )

    assert [item.kwargs["country"] for item in acquire.call_args_list] == ["A", "B"]
    selector.record_number_failure.assert_called_once_with("A")
    cancel.assert_called_once_with("id-a", http)


def test_actual_failures_are_recorded_and_second_failure_switches_country():
    selector = PreferredCountrySelector(["A", "B"], fallback_country="Z", failure_switch=2)
    selector.record_number_failure = Mock(wraps=selector.record_number_failure)
    http = Mock()
    patches = phone_flow_patches(
        send_side_effect=[RuntimeError("bad-a-1"), RuntimeError("bad-a-2"), None]
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "_cfg", cfg()))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "_http", return_value=http))
        stack.enter_context(patch.object(roxy_codex_oauth, "_build_sms_country_selector", return_value=selector))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "get_country_offers", return_value=[offer("A", "0.10"), offer("B", "0.20")]))
        acquire = stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "acquire_number", side_effect=[("id-a1", "111"), ("id-a2", "112"), ("id-b", "222")]))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "set_status"))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "wait_for_sms_code", return_value="123456"))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "complete"))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "cancel"))
        for item in patches:
            stack.enter_context(item)
        roxy_codex_oauth._do_phone_verification_if_present(object())

    assert [item.kwargs["country"] for item in acquire.call_args_list] == ["A", "A", "B"]
    assert selector.record_number_failure.call_args_list == [call("A"), call("A")]


@pytest.mark.parametrize("provider", ["l", "h"])
def test_fixed_provider_skips_price_api_and_passes_none_country(provider):
    provider_cfg = cfg(provider)
    http = Mock()
    patches = phone_flow_patches()
    with ExitStack() as stack:
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "_cfg", provider_cfg))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "_http", return_value=http))
        get_offers = stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "get_country_offers"))
        acquire = stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "acquire_number", return_value=("id", "123")))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "set_status"))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "wait_for_sms_code", return_value="123456"))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "complete"))
        stack.enter_context(patch.object(roxy_codex_oauth.sms_provider, "cancel"))
        for item in patches:
            stack.enter_context(item)
        roxy_codex_oauth._do_phone_verification_if_present(object())

    get_offers.assert_not_called()
    acquire.assert_called_once_with(http, country=None)


@pytest.mark.parametrize("provider", ["l", "h"])
def test_fixed_provider_no_numbers_retries_without_consuming_actual_attempt(provider):
    acquire = Mock(
        side_effect=[SmsNoNumbersError("NO_NUMBERS"), ("id", "123")]
    )

    with patch.object(roxy_codex_oauth.logger, "info") as info:
        acquire, _http, _cancel = run_phone_flow(
            cfg(provider, retries=5), acquire=acquire
        )

    assert [item.kwargs["country"] for item in acquire.call_args_list] == [None, None]
    attempt_logs = [
        args
        for args, _kwargs in info.call_args_list
        if "手机验证尝试" in str(args[0])
    ]
    assert attempt_logs == [
        (
            "[Codex][Browser] 手机验证尝试 %s/%s，provider=%s，号码=+%s",
            1,
            5,
            provider,
            "123",
        )
    ]


@pytest.mark.parametrize("provider", ["l", "h"])
def test_fixed_provider_repeated_no_numbers_stops_at_legacy_bound(provider):
    acquire = Mock(side_effect=SmsNoNumbersError("NO_NUMBERS"))

    with pytest.raises(SmsNoNumbersError):
        run_phone_flow(cfg(provider, retries=3), acquire=acquire)

    assert acquire.call_count == 3
    assert all(item.kwargs["country"] is None for item in acquire.call_args_list)
