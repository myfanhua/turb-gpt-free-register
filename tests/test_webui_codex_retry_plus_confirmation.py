# -*- coding: utf-8 -*-
import unittest
from concurrent.futures import Future
from unittest.mock import patch

from webui.app import create_app


class _CapturedThread:
    created = []

    def __init__(self, *, target, args=(), kwargs=None, **thread_kwargs):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.thread_kwargs = thread_kwargs
        self.__class__.created.append(self)

    def start(self):
        return None


class _ImmediateThread(_CapturedThread):
    def start(self):
        self.target(*self.args, **self.kwargs)


class _CapturedExecutor:
    submitted = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def submit(self, fn, *args, **kwargs):
        self.__class__.submitted.append((fn, args, kwargs))
        future = Future()
        future.set_result(None)
        return future


class WebUiCodexRetryPlusConfirmationTests(unittest.TestCase):
    def setUp(self):
        with patch("webui.app.db.recover_interrupted_extract_links", return_value={
            "failed_count": 0,
            "kakao_batches": [],
        }), patch(
            "webui.app.extract_link_service.resume_interrupted_kakao_batches",
            return_value={"resumed_batches": 0, "failed_batches": 0},
            create=True,
        ):
            self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        _CapturedThread.created.clear()
        _CapturedExecutor.submitted.clear()

    def _post_single(self, plus_confirmed):
        email = "single@example.com"
        with patch("webui.app.db.get_account_by_email", return_value={
            "id": 1,
            "email": email,
            "codex_status": "failed",
        }), patch("webui.app.codex_retry_service.reserve", return_value=True), patch(
            "webui.app.db.update_account_codex_status"
        ), patch("webui.app.threading.Thread", _CapturedThread):
            response = self.client.post("/api/codex/retry", json={
                "email": email,
                "plus_confirmed": plus_confirmed,
            })
        self.assertEqual(response.status_code, 200)
        return _CapturedThread.created[-1]

    def test_single_route_passes_only_json_boolean_true(self):
        thread = self._post_single(True)
        self.assertIs(thread.kwargs["plus_confirmed"], True)

        for value in (1, "true", [True], {"value": True}, None):
            with self.subTest(value=value):
                thread = self._post_single(value)
                self.assertIs(thread.kwargs["plus_confirmed"], False)

    def test_single_worker_forwards_confirmation_to_service(self):
        email = "single-worker@example.com"
        with patch("webui.app.db.get_account_by_email", return_value={
            "id": 2,
            "email": email,
            "codex_status": "failed",
        }), patch("webui.app.codex_retry_service.reserve", return_value=True), patch(
            "webui.app.db.update_account_codex_status"
        ), patch("webui.app.codex_retry_service.run_worker") as run_worker, patch(
            "webui.app.threading.Thread", _ImmediateThread
        ):
            response = self.client.post("/api/codex/retry", json={
                "email": email,
                "plus_confirmed": True,
            })

        self.assertEqual(response.status_code, 200)
        run_worker.assert_called_once_with(
            email,
            plus_confirmed=True,
            batch_label=None,
            clear_log=True,
        )

    def _post_bulk(self, plus_confirmed, *, thread_class=_CapturedThread):
        account = {"id": 7, "email": "bulk@example.com", "codex_status": "failed"}
        with patch("webui.app.db.get_account", return_value=account), patch(
            "webui.app.codex_retry_service.reserve", return_value=True
        ), patch("webui.app.db.update_account_codex_status"), patch(
            "webui.app.codex_retry_service.log_path"
        ) as log_path, patch("webui.app.threading.Thread", thread_class):
            log_path.return_value.parent.mkdir.return_value = None
            log_path.return_value.write_text.return_value = None
            response = self.client.post("/api/codex/retry-bulk", json={
                "account_ids": [7],
                "workers": 1,
                "plus_confirmed": plus_confirmed,
            })
        self.assertEqual(response.status_code, 200)
        return _CapturedThread.created[-1]

    def test_bulk_route_and_executor_pass_only_json_boolean_true(self):
        for value, expected in ((True, True), (1, False), ("true", False)):
            with self.subTest(value=value):
                dispatch_thread = self._post_bulk(value)
                self.assertIs(dispatch_thread.args[3], expected)
                _CapturedExecutor.submitted.clear()
                with patch("concurrent.futures.ThreadPoolExecutor", _CapturedExecutor):
                    self._post_bulk(value, thread_class=_ImmediateThread)
                worker, worker_args, worker_kwargs = _CapturedExecutor.submitted[-1]
                self.assertIs(worker_kwargs["plus_confirmed"], expected)
                with patch("webui.app.codex_retry_service.run_worker") as run_worker:
                    worker(*worker_args, **worker_kwargs)
                run_worker.assert_called_once_with(
                    "bulk@example.com",
                    plus_confirmed=expected,
                    batch_label=worker_kwargs["batch_label"],
                    clear_log=False,
                )


if __name__ == "__main__":
    unittest.main()
