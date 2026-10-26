"""协议后端及真实 wreq 的本地 HTTP 集成测试，不访问外部站点。"""
import importlib.util
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from requests.exceptions import HTTPError
from config import browser
from core.session import BrowserSession
from webui.config_editor import get_config, update_config


class ProtocolConfigTests(unittest.TestCase):
    def test_web_config_choices_and_default(self):
        field = next(f for f in get_config() if f['key'] == 'PROTOCOL_HTTP_CLIENT')
        self.assertEqual([c['value'] for c in field['choices']], ['curl_cffi', 'wreq'])

    def test_web_rejects_unknown_backend(self):
        with self.assertRaises(ValueError):
            update_config({'PROTOCOL_HTTP_CLIENT': 'unknown'})

    def test_curl_backend_preserved(self):
        with patch.object(browser, 'PROTOCOL_HTTP_CLIENT', 'curl_cffi'), \
             patch.object(browser, 'CURL_CFFI_IMPERSONATE', 'chrome146'), \
             patch('core.session.Session') as factory, \
             patch.object(BrowserSession, '_enforce_proxy_quality'):
            session = BrowserSession(proxy='', detect_exit_geo=False)
            self.assertIs(session.session, factory.return_value)
            factory.assert_called_once_with(impersonate='chrome146')
            session.close()


@unittest.skipUnless(importlib.util.find_spec('wreq'), '可选依赖 wreq 未安装')
class WreqIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/echo')
                    self.send_header('Set-Cookie', 'redirect_cookie=yes; Path=/; HttpOnly')
                    self.end_headers()
                    return
                if self.path == '/blocked':
                    self.send_response(403)
                    self.send_header('cf-mitigated', 'challenge')
                    self.end_headers()
                    self.wfile.write(b'blocked')
                    return
                self.respond({'cookie': self.headers.get('Cookie', ''), 'path': self.path})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0))).decode()
                self.respond({'body': body, 'type': self.headers.get('Content-Type')})

            def respond(self, payload):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        from core.wreq_session import WreqSession
        self.client = WreqSession('chrome146')
        self.addCleanup(self.client.close)

    def test_cookie_redirect_and_case_insensitive_headers(self):
        self.client.cookies.set('device', 'abc', domain='127.0.0.1')
        response = self.client.get(self.url + '/redirect')
        self.assertEqual(response.status_code, 200)
        self.assertIn('redirect_cookie=yes', response.json()['cookie'])
        self.assertIn('device=abc', response.json()['cookie'])
        self.assertIn('application/json', response.headers['content-type'])
        self.assertEqual(self.client.cookies.get('redirect_cookie'), 'yes')
        self.assertTrue(response.url.endswith('/echo'))
        self.assertEqual(response.content.decode(), response.text)

    def test_no_redirect_and_params(self):
        response = self.client.get(self.url + '/redirect', allow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['location'], '/echo')
        self.assertIn('q=a+b', self.client.get(self.url + '/echo', params={'q': 'a b'}).json()['path'])

    def test_host_only_response_cookie_has_the_response_scope(self):
        response = self.client.get(self.url + '/redirect')
        cookie = next(c for c in response.cookies if c.name == 'redirect_cookie')
        self.assertEqual(cookie.domain, '127.0.0.1')
        self.assertFalse(cookie.domain_specified)
        self.assertTrue(cookie.has_nonstandard_attr('HttpOnly'))

    def test_form_raw_and_json_bodies(self):
        self.assertEqual(self.client.post(self.url, data={'name': 'a b'}).json()['body'], 'name=a+b')
        self.assertEqual(self.client.post(self.url, data='raw').json()['body'], 'raw')
        response = self.client.post(self.url, json={'ok': True})
        self.assertEqual(json.loads(response.json()['body']), {'ok': True})

    def test_http_error_has_response(self):
        response = self.client.get(self.url + '/blocked')
        with self.assertRaises(HTTPError) as error:
            response.raise_for_status()
        self.assertIs(error.exception.response, response)
        self.assertEqual(response.headers['CF-Mitigated'], 'challenge')

    def test_browser_session_backend_and_circuit_breaker(self):
        with patch.object(browser, 'PROTOCOL_HTTP_CLIENT', 'wreq'), \
             patch.object(BrowserSession, '_enforce_proxy_quality'):
            session = BrowserSession(proxy='', detect_exit_geo=False)
            self.addCleanup(session.close)
            self.assertEqual(session.http_client, 'wreq')
            self.assertEqual(session.get(self.url + '/blocked').status_code, 403)
            with self.assertRaisesRegex(RuntimeError, '熔断'):
                session.get(self.url + '/echo')

    def test_proxy_is_used(self):
        # 将本地 HTTP 测试服务当作 HTTP 代理，检查收到的绝对 URL。
        self.client.proxies = {'http': self.url}
        response = self.client.get('http://unresolvable.invalid/echo')
        self.assertEqual(response.json()['path'], 'http://unresolvable.invalid/echo')

    def test_closed_session_and_unknown_arguments(self):
        with self.assertRaises(TypeError):
            self.client.get(self.url, unsupported=True)
        self.client.close()
        with self.assertRaisesRegex(RuntimeError, '已关闭'):
            self.client.get(self.url)

    def test_wreq_requests_do_not_emit_transport_logs(self):
        with self.assertNoLogs('core', level='DEBUG'):
            response = self.client.post(self.url + '/echo', json={'ok': True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.json()['body']), {'ok': True})

    def test_curl_uses_native_session_without_transport_logs(self):
        from curl_cffi.requests import Session as NativeSession
        from core.session import Session
        self.assertIs(Session, NativeSession)
        with Session(impersonate='chrome146') as client:
            with self.assertNoLogs('core', level='DEBUG'):
                response = client.post(self.url + '/echo', json={'ok': True})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(response.json()['body']), {'ok': True})
