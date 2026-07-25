import unittest
from unittest.mock import Mock, patch
from core import generic_api_mail_client as c
from core.otp_wait_policy import OTPProbeResult

class GenericApiProbeTests(unittest.TestCase):
    def setUp(self): c._CONTEXT_CACHE["a@test.com"] = c.GenericApiEmailAccount("a@test.com", "https://code.test")
    def tearDown(self): c._CONTEXT_CACHE.clear()
    @patch("core.generic_api_mail_client.requests.get")
    @patch("core.generic_api_mail_client.time.monotonic", side_effect=[0, 5])
    def test_pending_candidate_and_settle_completed(self, clock, get):
        r=Mock(status_code=200, text="verification code 123456"); get.return_value=r
        state=c.GenericApiProbeState()
        first=c.fetch_otp_once("a@test.com", 0, state, settle_seconds=5)
        self.assertEqual(first.status, "candidate")
        second=c.fetch_otp_once("a@test.com", 0, state, settle_seconds=5)
        self.assertEqual(second.status, "completed")
        self.assertEqual(second.code, "123456")
    @patch("core.generic_api_mail_client.requests.get")
    def test_empty_response_is_pending(self, get):
        get.return_value=Mock(status_code=200, text="no mail")
        self.assertEqual(c.fetch_otp_once("a@test.com", 0, c.GenericApiProbeState()).status, "pending")
    def test_assurivo_url_validation_keeps_encoded_credential_and_rejects_other_mail(self):
        url="https://assurivo.com/console/open.php?mail=a%40test.com&pwd=a%26b&limit=5"
        self.assertEqual(c.validate_code_url("a@test.com", url), url)
        with self.assertRaisesRegex(c.GenericApiMailError, "mail 参数"):
            c.validate_code_url("a@test.com", "https://assurivo.com/console/open.php?mail=other%40test.com&pwd=secret")
    @patch("core.generic_api_mail_client.requests.get")
    def test_assurivo_fixture_uses_openai_parser_without_logging_credential(self, get):
        c._CONTEXT_CACHE["a@test.com"] = c.GenericApiEmailAccount("a@test.com", "https://assurivo.com/console/open.php?mail=a%40test.com&pwd=secret")
        get.return_value=Mock(status_code=200); get.return_value.json.return_value={"emails":[{"from":"noreply@openai.com","subject":"Verification code","text":"654321","timestamp":200}]}
        result=c.fetch_otp_once("a@test.com", 100, c.GenericApiProbeState(), settle_seconds=0)
        self.assertEqual(result.status,"completed")
        self.assertEqual(get.call_args.args[0], c._CONTEXT_CACHE["a@test.com"].code_url)

if __name__ == "__main__": unittest.main()
