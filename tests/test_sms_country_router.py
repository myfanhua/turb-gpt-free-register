# -*- coding: utf-8 -*-
from decimal import Decimal

import pytest

from core.sms_country_router import NoEligibleSmsCountry, PreferredCountrySelector
from core.sms_provider import SmsCountryOffer


def offer(country: str, price: str, count: int = 1) -> SmsCountryOffer:
    return SmsCountryOffer(country, Decimal(price), count)


def test_choose_uses_cheapest_in_stock_preferred_country():
    selector = PreferredCountrySelector(["33", "187"], fallback_country="6")

    chosen = selector.choose([offer("33", "0.11", 7), offer("187", "0.09", 2)])

    assert chosen == "187"
    assert selector.current_country == "187"
    assert selector.last_reason == "lowest_price"
    assert selector.needs_offer_refresh is False


def test_equal_prices_use_normalized_saved_preference_order():
    selector = PreferredCountrySelector(
        [" 187 ", "33", "187", "", "33"], fallback_country="6"
    )

    chosen = selector.choose([offer("33", "0.10"), offer("187", "0.10")])

    assert selector.preferred_countries == ["187", "33"]
    assert chosen == "187"


def test_first_number_failure_reuses_country_then_threshold_switches():
    selector = PreferredCountrySelector(["33", "187"], fallback_country="6")
    offers = [offer("33", "0.10"), offer("187", "0.11")]

    assert selector.choose(offers) == "33"
    assert selector.record_number_failure("33") == 1
    assert selector.failure_count("33") == 1
    assert selector.current_country == "33"
    assert selector.needs_offer_refresh is False

    assert selector.choose(offers) == "33"
    assert selector.last_reason == "same_country_second_attempt"

    assert selector.record_number_failure("33") == 2
    assert selector.current_country is None
    assert selector.needs_offer_refresh is True
    assert selector.choose(offers) == "187"
    assert selector.last_reason == "lowest_price"
    assert selector.needs_offer_refresh is False


def test_failures_route_a_a_b_b_a_across_cycles():
    selector = PreferredCountrySelector(["A", "B"], fallback_country="A")
    offers = [offer("A", "0.10"), offer("B", "0.11")]

    route = []
    for _ in range(5):
        country = selector.choose(offers)
        route.append(country)
        selector.record_number_failure(country)

    assert route == ["A", "A", "B", "B", "A"]
    assert selector.failure_count("A") == 1
    assert selector.failure_count("B") == 0


def test_no_numbers_marks_unavailable_switches_and_does_not_increment():
    selector = PreferredCountrySelector(["33", "187"], fallback_country="6")
    offers = [offer("33", "0.10"), offer("187", "0.11")]

    assert selector.choose(offers) == "33"
    selector.record_no_numbers("33")

    assert selector.failure_count("33") == 0
    assert selector.current_country is None
    assert selector.needs_offer_refresh is True
    assert selector.choose(offers) == "187"
    assert selector.last_reason == "lowest_price"


def test_error_reasons_cover_over_price_and_zero_stock():
    selector = PreferredCountrySelector(
        ["33", "187"], fallback_country="6", max_price="0.15"
    )

    with pytest.raises(NoEligibleSmsCountry) as raised:
        selector.choose([offer("33", "0.16", 4), offer("187", "0.10", 0)])

    assert raised.value.reasons == {
        "33": "over_max_price",
        "187": "no_stock",
    }
    assert "33=over_max_price" in str(raised.value)
    assert "187=no_stock" in str(raised.value)


def test_empty_preferred_countries_use_normalized_fallback_country():
    selector = PreferredCountrySelector([], fallback_country=" 187 ")

    assert selector.preferred_countries == ["187"]
    assert selector.choose([offer("187", "0.12")]) == "187"


def test_missing_live_offers_can_use_saved_order_fallback():
    selector = PreferredCountrySelector(["33", "187"], fallback_country="6")

    assert selector.choose(None, allow_order_fallback=True) == "33"
    assert selector.current_country == "33"
    assert selector.last_reason == "saved_order_fallback"
    assert selector.needs_offer_refresh is False


def test_missing_live_offers_preserve_current_country_second_attempt():
    selector = PreferredCountrySelector(["A", "B"], fallback_country="A")

    assert selector.choose([offer("A", "0.20"), offer("B", "0.10")]) == "B"
    assert selector.record_number_failure("B") == 1

    assert selector.choose(None, allow_order_fallback=True) == "B"
    assert selector.current_country == "B"
    assert selector.last_reason == "same_country_second_attempt"


def test_missing_live_offers_without_fallback_reports_no_quotes():
    selector = PreferredCountrySelector(["33", "187"], fallback_country="6")

    with pytest.raises(NoEligibleSmsCountry) as raised:
        selector.choose(None)

    assert raised.value.reasons == {"33": "no_quote", "187": "no_quote"}
    assert selector.current_country is None
    assert selector.needs_offer_refresh is True


def test_unrequested_offers_are_ignored():
    selector = PreferredCountrySelector(["33"], fallback_country="6")

    with pytest.raises(NoEligibleSmsCountry) as raised:
        selector.choose([offer("6", "0.01", 99)])

    assert raised.value.reasons == {"33": "no_quote"}


@pytest.mark.parametrize("max_price", ["", "not-a-price", "NaN", None])
def test_blank_or_invalid_max_price_means_no_cap(max_price):
    selector = PreferredCountrySelector(
        ["33"], fallback_country="6", max_price=max_price
    )

    assert selector.choose([offer("33", "999.99")]) == "33"


def test_failure_switch_has_minimum_of_one():
    selector = PreferredCountrySelector(
        ["A", "B"], fallback_country="A", failure_switch=0
    )
    offers = [offer("A", "0.10"), offer("B", "0.11")]

    assert selector.choose(offers) == "A"
    assert selector.record_number_failure("A") == 1
    assert selector.choose(offers) == "B"


def test_no_number_exclusions_survive_failure_cycle_reset():
    selector = PreferredCountrySelector(
        ["A", "B", "C"], fallback_country="A", failure_switch=1
    )
    offers = [
        offer("A", "0.09"),
        offer("B", "0.10"),
        offer("C", "0.11"),
    ]

    assert selector.choose(offers) == "A"
    selector.record_no_numbers("A")
    assert selector.choose(offers) == "B"
    selector.record_number_failure("B")
    assert selector.choose(offers) == "C"
    selector.record_number_failure("C")

    assert selector.choose(offers) == "B"
    assert selector.failure_count("B") == 0
    assert selector.failure_count("C") == 0

    selector.record_number_failure("B")
    selector.record_number_failure("C")
    with pytest.raises(NoEligibleSmsCountry) as raised:
        selector.choose([offer("A", "0.09")])
    assert raised.value.reasons["A"] == "no_numbers"


def test_cycle_reset_uses_current_offer_eligibility():
    selector = PreferredCountrySelector(
        ["A", "B"], fallback_country="A", failure_switch=1
    )
    live = [offer("A", "0.10")]

    assert selector.choose(live) == "A"
    selector.record_number_failure("A")

    assert selector.choose(live) == "A"
    assert selector.failure_count("A") == 0


def test_failure_threshold_reason_is_reported_without_live_offers():
    selector = PreferredCountrySelector(
        ["A", "B"], fallback_country="A", failure_switch=1
    )
    assert selector.choose([offer("A", "0.10")]) == "A"
    selector.record_number_failure("A")

    with pytest.raises(NoEligibleSmsCountry) as raised:
        selector.choose(None)

    assert raised.value.reasons == {"A": "failure_threshold", "B": "no_quote"}


def test_reuse_requires_current_country_to_remain_eligible():
    selector = PreferredCountrySelector(["A", "B"], fallback_country="A")

    assert selector.choose([offer("A", "0.10"), offer("B", "0.20")]) == "A"
    selector.record_number_failure("A")

    assert selector.choose([offer("A", "0.10", 0), offer("B", "0.20")]) == "B"
    assert selector.last_reason == "lowest_price"
