import unittest
from unittest.mock import MagicMock, patch

from core.roxy_registration import run_roxy_registration


class RoxyTaskProxyBindingTests(unittest.TestCase):
    def test_registration_passes_task_proxy_into_roxy_profile(self):
        task_proxy = "socks5h://user:pass@proxy.example.test:1080"
        opened = MagicMock()
        opened.profile_id = "test-profile"
        client = MagicMock()
        client.open_profile.return_value = opened

        with patch("core.roxy_registration.RoxyBrowserClient", return_value=client), patch(
            "core.roxy_registration._build_driver",
            side_effect=RuntimeError("stop-after-profile-open"),
        ):
            run_roxy_registration(
                "user@example.test",
                "Test User",
                "1990-01-01",
                proxy=task_proxy,
            )

        client.open_profile.assert_called_once_with(proxy=task_proxy)


if __name__ == "__main__":
    unittest.main()
