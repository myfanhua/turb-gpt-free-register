import threading
import time
import unittest
from unittest.mock import patch

from core import roxy_registration
from core.roxybrowser_client import RoxyOpenResult


class _FakeClient:
    def __init__(self, name, events):
        self.name = name
        self.events = events

    def open_profile(self):
        self.events.append(f"open-{self.name}")
        return RoxyOpenResult(profile_id=self.name, raw={})

    def cleanup_profile(self, opened):
        self.events.append(f"cleanup-{opened.profile_id}")


class _FakeDriver:
    def __init__(self, name, events, first_navigation_started, release_first):
        self.name = name
        self.events = events
        self.first_navigation_started = first_navigation_started
        self.release_first = release_first

    def set_page_load_timeout(self, seconds):
        self.events.append(f"timeout-{self.name}-{seconds}")

    def get(self, url):
        self.events.append(f"get-start-{self.name}")
        if self.name == "first":
            self.first_navigation_started.set()
            self.release_first.wait(timeout=2)
        self.events.append(f"get-end-{self.name}")

    def quit(self):
        self.events.append(f"quit-{self.name}")


class RoxyStartupGateTests(unittest.TestCase):
    def test_second_registration_waits_until_first_login_page_finishes_loading(self):
        events = []
        first_navigation_started = threading.Event()
        release_first = threading.Event()
        second_startup_attempted = threading.Event()
        clients = {
            name: _FakeClient(name, events)
            for name in ("first", "second")
        }
        drivers = {
            name: _FakeDriver(
                name,
                events,
                first_navigation_started,
                release_first,
            )
            for name in ("first", "second")
        }
        results = {}

        def wait_for_email_input(driver, timeout=None):
            events.append(f"email-ready-{driver.name}")
            return object()

        def run(name):
            if name == "second":
                second_startup_attempted.set()
            results[name] = roxy_registration._open_roxy_registration_browser(
                clients[name]
            )

        with patch.object(
            roxy_registration,
            "_build_driver",
            side_effect=lambda opened: drivers[opened.profile_id],
        ), patch.object(roxy_registration, "_center_browser_window"), patch.object(
            roxy_registration, "human_delay"
        ), patch.object(roxy_registration, "_maybe_accept"), patch.object(
            roxy_registration,
            "_wait_for_email_input_ready",
            side_effect=wait_for_email_input,
        ):
            first = threading.Thread(target=run, args=("first",))
            second = threading.Thread(target=run, args=("second",))
            first.start()
            self.assertTrue(first_navigation_started.wait(timeout=1))
            second.start()
            self.assertTrue(second_startup_attempted.wait(timeout=1))
            time.sleep(0.05)

            self.assertNotIn("open-second", events)

            release_first.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertLess(events.index("email-ready-first"), events.index("open-second"))
        self.assertLess(events.index("get-end-first"), events.index("open-second"))
        self.assertEqual(results["first"][0].profile_id, "first")
        self.assertEqual(results["second"][0].profile_id, "second")

    def test_startup_abort_still_closes_driver_and_cleans_profile(self):
        class StartupAbort(BaseException):
            pass

        events = []
        client = _FakeClient("aborted", events)
        driver = _FakeDriver(
            "aborted",
            events,
            threading.Event(),
            threading.Event(),
        )
        driver.get = lambda _url: (_ for _ in ()).throw(StartupAbort())

        with patch.object(roxy_registration, "_build_driver", return_value=driver), \
             patch.object(roxy_registration, "_center_browser_window"), \
             patch.object(roxy_registration, "_check_manual_stop"), \
             patch.object(roxy_registration._cfg, "ROXY_KEEP_BROWSER_OPEN", False):
            with self.assertRaises(StartupAbort):
                roxy_registration._open_roxy_registration_browser(client)

        self.assertEqual(events.count("quit-aborted"), 1)
        self.assertEqual(events.count("cleanup-aborted"), 1)


if __name__ == "__main__":
    unittest.main()
