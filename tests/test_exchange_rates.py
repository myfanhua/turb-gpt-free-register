import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from core import exchange_rates


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ExchangeRateTests(unittest.TestCase):
    def tearDown(self):
        exchange_rates._reset_cache_for_tests()

    def test_latest_rate_is_parsed_and_cached(self):
        response = _Response({"base": "USD", "date": "2026-05-21", "rates": {"CNY": 7.2}})
        with patch("core.exchange_rates.urlopen", return_value=response) as urlopen:
            self.assertEqual(exchange_rates.get_usd_cny_rate(), (Decimal("7.2"), "2026-05-21"))
            self.assertEqual(exchange_rates.get_usd_cny_rate(), (Decimal("7.2"), "2026-05-21"))
        self.assertEqual(urlopen.call_count, 1)
        self.assertIn("base=USD", urlopen.call_args.args[0].full_url)

    def test_network_failure_returns_no_fabricated_rate_and_is_cooled_down(self):
        with patch("core.exchange_rates.urlopen", side_effect=OSError("offline")) as urlopen:
            self.assertIsNone(exchange_rates.get_usd_cny_rate())
            self.assertIsNone(exchange_rates.get_usd_cny_rate())
        self.assertEqual(urlopen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
