import threading
import unittest

from core import registration_service


class RegistrationExecutorTests(unittest.TestCase):
    def setUp(self):
        registration_service.shutdown_executor(wait=True)

    def tearDown(self):
        registration_service.shutdown_executor(wait=True)

    def test_worker_change_waits_for_previous_generation_to_drain(self):
        old_executor = registration_service.get_executor(1)
        started = threading.Event()
        release = threading.Event()
        switched = threading.Event()
        replacement = []

        def blocked_job():
            started.set()
            self.assertTrue(release.wait(2))

        old_future = old_executor.submit(blocked_job)
        self.assertTrue(started.wait(1))

        def switch_workers():
            replacement.append(registration_service.get_executor(2))
            switched.set()

        switch_thread = threading.Thread(target=switch_workers)
        switch_thread.start()
        self.assertFalse(switched.wait(0.1))

        release.set()
        self.assertTrue(switched.wait(2))
        switch_thread.join(1)
        old_future.result(timeout=1)

        self.assertEqual(len(replacement), 1)
        self.assertIsNot(replacement[0], old_executor)
        self.assertEqual(registration_service.get_executor_workers(), 2)


if __name__ == "__main__":
    unittest.main()
