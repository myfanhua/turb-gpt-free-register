import ast
import unittest
from pathlib import Path

from config import extract_link
from webui.config_editor import EDITABLE_FIELDS


class KakaoExtractConfigTests(unittest.TestCase):
    def test_defaults_preserve_legacy_provider(self):
        source = Path(extract_link.__file__).read_text(encoding="utf-8")
        defaults = {
            node.target.id: ast.literal_eval(node.value)
            for node in ast.parse(source).body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        }

        self.assertEqual(defaults["EXTRACT_LINK_PROVIDER"], "legacy")
        self.assertEqual(defaults["KAKAO_EXTRACT_BATCH_SIZE"], 5)
        self.assertEqual(
            defaults["KAKAO_EXTRACT_API_BASE"],
            "https://tiqu.dxmcs.xin",
        )
        self.assertEqual(defaults["KAKAO_EXTRACT_TIMEOUT_SECONDS"], 930)
        self.assertEqual(defaults["KAKAO_EXTRACT_POLL_INTERVAL"], 4.0)

    def test_kakao_cdk_is_a_separate_secret_field(self):
        fields = {field["key"]: field for field in EDITABLE_FIELDS}

        self.assertTrue(fields["EXTRACT_LINK_CDK"]["secret"])
        self.assertTrue(fields["KAKAO_EXTRACT_CDK"]["secret"])
        self.assertNotEqual(
            fields["EXTRACT_LINK_CDK"]["key"],
            fields["KAKAO_EXTRACT_CDK"]["key"],
        )

    def test_kakao_fields_remain_in_extract_link_group(self):
        fields = {field["key"]: field for field in EDITABLE_FIELDS}

        for key in (
            "EXTRACT_LINK_PROVIDER",
            "KAKAO_EXTRACT_API_BASE",
            "KAKAO_EXTRACT_CDK",
            "KAKAO_EXTRACT_BATCH_SIZE",
            "KAKAO_EXTRACT_TIMEOUT_SECONDS",
            "KAKAO_EXTRACT_POLL_INTERVAL",
        ):
            self.assertEqual(fields[key]["group"], "提链")


if __name__ == "__main__":
    unittest.main()
