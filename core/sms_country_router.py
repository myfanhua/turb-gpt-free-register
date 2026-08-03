# -*- coding: utf-8 -*-
"""Pure per-task routing across preferred SMS countries."""

from decimal import Decimal, InvalidOperation

from core.sms_provider import SmsCountryOffer


class NoEligibleSmsCountry(RuntimeError):
    """Raised when every preferred country is unavailable for this attempt."""

    def __init__(self, reasons: dict[str, str]):
        self.reasons = dict(reasons)
        details = ", ".join(f"{code}={reason}" for code, reason in reasons.items())
        super().__init__(f"No eligible SMS country: {details or 'no preferred countries'}")


class PreferredCountrySelector:
    """Keep the routing state for one Codex registration task."""

    def __init__(
        self,
        preferred_countries,
        *,
        fallback_country,
        failure_switch=2,
        max_price="",
    ):
        if isinstance(preferred_countries, str):
            preferred_countries = [preferred_countries]

        normalized = []
        seen = set()
        for raw in preferred_countries or []:
            code = str(raw or "").strip()
            if code and code not in seen:
                normalized.append(code)
                seen.add(code)

        fallback = str(fallback_country or "").strip()
        if not normalized and fallback:
            normalized.append(fallback)

        try:
            switch = int(failure_switch)
        except (TypeError, ValueError):
            switch = 2

        self.preferred_countries = normalized
        self.current_country: str | None = None
        self.needs_offer_refresh = True
        self.last_reason = ""
        self._failure_switch = max(1, switch)
        self._max_price = self._parse_max_price(max_price)
        self._failure_counts: dict[str, int] = {}
        self._failure_blocked: set[str] = set()
        self._no_numbers: set[str] = set()

    @staticmethod
    def _parse_max_price(raw) -> Decimal | None:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            price = Decimal(text)
        except (InvalidOperation, ValueError):
            return None
        return price if price.is_finite() else None

    def failure_count(self, country) -> int:
        return self._failure_counts.get(str(country or "").strip(), 0)

    def record_number_failure(self, country) -> int:
        code = str(country or "").strip()
        count = self.failure_count(code) + 1
        self._failure_counts[code] = count
        if count >= self._failure_switch:
            self._failure_blocked.add(code)
            if self.current_country == code:
                self.current_country = None
            self.needs_offer_refresh = True
            self.last_reason = "failure_threshold"
        else:
            self.last_reason = "same_country_second_attempt"
        return count

    def record_no_numbers(self, country) -> None:
        code = str(country or "").strip()
        self._no_numbers.add(code)
        self._failure_blocked.discard(code)
        if self.current_country == code:
            self.current_country = None
        self.needs_offer_refresh = True
        self.last_reason = "no_numbers"

    def choose(
        self,
        offers: list[SmsCountryOffer] | None,
        *,
        allow_order_fallback: bool = False,
    ) -> str:
        if offers is None:
            if allow_order_fallback:
                self._restart_failure_cycle_if_exhausted(
                    [
                        code
                        for code in self.preferred_countries
                        if code not in self._no_numbers
                    ]
                )
                if (
                    self.current_country in self.preferred_countries
                    and 0
                    < self.failure_count(self.current_country)
                    < self._failure_switch
                    and self.current_country not in self._no_numbers
                    and self.current_country not in self._failure_blocked
                ):
                    return self._select(
                        self.current_country, "same_country_second_attempt"
                    )
                for code in self.preferred_countries:
                    if code not in self._no_numbers and code not in self._failure_blocked:
                        return self._select(code, "saved_order_fallback")
            self.current_country = None
            self.needs_offer_refresh = True
            self.last_reason = "no_eligible_country"
            raise NoEligibleSmsCountry(self._reasons_without_offers())

        offer_by_country = {
            str(item.country_code).strip(): item
            for item in offers
            if str(item.country_code).strip() in self.preferred_countries
        }
        self._restart_failure_cycle_if_exhausted(
            self._offer_eligible_countries(offer_by_country)
        )
        eligible, reasons = self._eligible_offers(offer_by_country)

        if (
            self.current_country
            and 0 < self.failure_count(self.current_country) < self._failure_switch
            and self.current_country in eligible
        ):
            return self._select(self.current_country, "same_country_second_attempt")

        if not eligible:
            self.current_country = None
            self.needs_offer_refresh = False
            self.last_reason = "no_eligible_country"
            raise NoEligibleSmsCountry(reasons)

        preference_index = {
            code: index for index, code in enumerate(self.preferred_countries)
        }
        code = min(
            eligible,
            key=lambda candidate: (
                eligible[candidate].price,
                preference_index[candidate],
            ),
        )
        return self._select(code, "lowest_price")

    def _select(self, code: str, reason: str) -> str:
        self.current_country = code
        self.needs_offer_refresh = False
        self.last_reason = reason
        return code

    def _restart_failure_cycle_if_exhausted(self, cycle_countries: list[str]) -> None:
        if cycle_countries and all(
            code in self._failure_blocked for code in cycle_countries
        ):
            for code in cycle_countries:
                self._failure_counts[code] = 0
                self._failure_blocked.discard(code)

    def _offer_eligible_countries(
        self, offer_by_country: dict[str, SmsCountryOffer]
    ) -> list[str]:
        countries = []
        for code in self.preferred_countries:
            offer = offer_by_country.get(code)
            if code in self._no_numbers or offer is None or offer.available_count <= 0:
                continue
            if self._max_price is not None and offer.price > self._max_price:
                continue
            countries.append(code)
        return countries

    def _reasons_without_offers(self) -> dict[str, str]:
        reasons = {}
        for code in self.preferred_countries:
            if code in self._no_numbers:
                reasons[code] = "no_numbers"
            elif code in self._failure_blocked:
                reasons[code] = "failure_threshold"
            else:
                reasons[code] = "no_quote"
        return reasons

    def _eligible_offers(
        self, offer_by_country: dict[str, SmsCountryOffer]
    ) -> tuple[dict[str, SmsCountryOffer], dict[str, str]]:
        eligible = {}
        reasons = {}
        for code in self.preferred_countries:
            offer = offer_by_country.get(code)
            if code in self._no_numbers:
                reasons[code] = "no_numbers"
            elif offer is None:
                reasons[code] = "no_quote"
            elif offer.available_count <= 0:
                reasons[code] = "no_stock"
            elif self._max_price is not None and offer.price > self._max_price:
                reasons[code] = "over_max_price"
            elif code in self._failure_blocked:
                reasons[code] = "failure_threshold"
            else:
                eligible[code] = offer
        return eligible, reasons
