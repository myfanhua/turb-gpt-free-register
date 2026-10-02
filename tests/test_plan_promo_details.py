import copy
import json
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from core import db
from core.chatgpt_plan import parse_accounts_check
from webui.app import _compact_account_for_list


class PlanPromoDetailsTests(unittest.TestCase):
    def parse(self, campaigns, plan="free"):
        return parse_accounts_check({"accounts": {"default": {
            "account": {"plan_type": plan},
            "eligible_promo_campaigns": campaigns,
        }}})

    def test_all_campaigns_preserved_without_changing_plus_logic(self):
        campaigns = {
            "plus": {"id": "plus-1-month-free", "metadata": {
                "discount": {"percentage": 100},
                "duration": {"num_periods": 1, "period": "month"},
                "no_auto_renewal_at_discount_end": False,
            }},
            "go": {"id": "go-discount", "metadata": {"custom_field": "保留未知字段"}},
        }
        result = self.parse(campaigns)
        self.assertTrue(result["plus_trial_eligible"])
        self.assertEqual(result["eligible_promo_campaigns"], campaigns)
        self.assertEqual(result["plus_trial_discount_percentage"], 100)
        other = self.parse({"go": campaigns["go"]})
        self.assertFalse(other["plus_trial_eligible"])
        self.assertIn("go", other["eligible_promo_campaigns"])
        self.assertEqual(self.parse(campaigns, "plus")["eligible_promo_campaigns"], {})
        self.assertEqual(self.parse({})["eligible_promo_campaigns"], {})

    def test_persistence_list_polling_and_failure_preservation(self):
        rows = [{"id": 1, "email": "test@example.test"}]
        result = self.parse({"go": {"id": "go-offer"}})
        with patch.object(db, "_load_accounts", return_value=rows), patch.object(db, "_save_accounts"):
            db.update_account_plan_check(acc_id=1, result=result)
            self.assertEqual(rows[0]["eligible_promo_campaigns"], result["eligible_promo_campaigns"])
            self.assertEqual(_compact_account_for_list(rows[0])["eligible_promo_campaigns"], result["eligible_promo_campaigns"])
            with patch.object(db, "_query_collection_page", side_effect=lambda *a, **kw: (copy.deepcopy(rows), 1, "")):
                snapshot = db.list_account_plan_check_statuses()
                self.assertEqual(snapshot["items"][0]["eligible_promo_campaigns"], result["eligible_promo_campaigns"])
                rows[0]["eligible_promo_campaigns"] = {"go": {"id": "changed"}}
                self.assertNotEqual(snapshot["revision"], db.list_account_plan_check_statuses()["revision"])
            db.update_account_plan_check(acc_id=1, result={"ok": False, "error": "timeout"})
            self.assertEqual(rows[0]["eligible_promo_campaigns"], {"go": {"id": "changed"}})
            db.update_account_plan_check(acc_id=1, result=self.parse({}))
            self.assertEqual(rows[0]["eligible_promo_campaigns"], {})

    def test_codex_phone_price_uses_live_usd_cny_rate_only_on_success(self):
        row = {
            "id": 7, "email": "codex@example.test", "codex_status": "success",
            "extra_json": json.dumps({"codex": {"phone_activation": {
                "phone_number": "+15550001111", "country": "36",
                "price_amount": "0.025", "price_currency": "USD",
                "price_source": "SMSBower getPrices 报价",
            }}}),
        }
        with patch("webui.app._codex_phone_region", return_value="美国"), patch("webui.app.exchange_rates.get_usd_cny_rate", return_value=(Decimal("7.2"), "2026-05-21")):
            compact = _compact_account_for_list(row)
        self.assertEqual(compact["codex_account_info"], {
            "phone_number": "+15550001111", "region": "美国",
            "price_usd": "0.025", "price_cny": "0.18",
            "rate_date": "2026-05-21", "price_source": "SMSBower getPrices 报价",
        })
        failed = dict(row, codex_status="failed")
        self.assertNotIn("codex_account_info", _compact_account_for_list(failed))

    def test_codex_phone_price_converts_from_cny_and_keeps_known_price_on_fx_failure(self):
        activation = {"phone_number": "+8613800138000", "price_amount": "0.18", "price_currency": "CNY"}
        row = {"codex_status": "success", "extra_json": json.dumps({"codex": {"phone_activation": activation}})}
        with patch("webui.app._codex_phone_region", return_value="中国"), patch("webui.app.exchange_rates.get_usd_cny_rate", return_value=(Decimal("7.2"), "2026-05-21")):
            info = _compact_account_for_list(row)["codex_account_info"]
        self.assertEqual((info["price_cny"], info["price_usd"]), ("0.18", "0.025"))
        with patch("webui.app._codex_phone_region", return_value="中国"), patch("webui.app.exchange_rates.get_usd_cny_rate", return_value=None):
            info = _compact_account_for_list(row)["codex_account_info"]
        self.assertEqual(info["price_cny"], "0.18")
        self.assertNotIn("price_usd", info)

    def test_codex_phone_info_ignores_unrelated_or_malformed_snapshots(self):
        row = {"codex_status": "success", "extra_json": "{not-json}", "plan_check_result_json": json.dumps({"billing_region": "JP", "price_usd": 19.99})}
        self.assertNotIn("codex_account_info", _compact_account_for_list(row))

    def test_codex_status_update_merges_phone_snapshot_into_extra_json(self):
        rows = [{
            "email": "merge@example.test", "codex_status": "retrying",
            "extra_json": json.dumps({"keep": 1, "codex": {"existing": True}}),
        }]
        snapshot = {"phone_number": "+15550001111", "price_amount": "0.02", "price_currency": "USD"}
        with patch.object(db, "_load_accounts", return_value=rows), patch.object(db, "_save_accounts"):
            self.assertTrue(db.update_account_codex_status("merge@example.test", "success", None, snapshot))
        extra = json.loads(rows[0]["extra_json"])
        self.assertEqual(extra["keep"], 1)
        self.assertTrue(extra["codex"]["existing"])
        self.assertEqual(extra["codex"]["phone_activation"], snapshot)

    def test_codex_account_info_frontend_uses_phone_region_price_lines(self):
        template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        content = template.read_text(encoding="utf-8")
        renderer = content.split("function _codexAccountInfoCellV2(r)", 1)[1].split("function _codexCellV2(r)", 1)[0]
        self.assertIn("手机号：", renderer)
        self.assertIn("归属地：", renderer)
        self.assertIn("价格：", renderer)
        self.assertIn("priceParts.join('/')", renderer)
        money_formatter = content.split("function _codexMoneyLabel(value, symbol)", 1)[1].split("function _codexAccountInfoCellV2(r)", 1)[0]
        self.assertIn("return amount ? amount + symbol : '—';", money_formatter)
        self.assertIn("if (!phone) return '';", renderer)
        self.assertIn("if (String(r.codex_status || '').toLowerCase() !== 'success') return '';", renderer)


if __name__ == "__main__":
    unittest.main()
