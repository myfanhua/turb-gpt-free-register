"""Cookie 身份读取必须与请求目标作用域一致；全部离线。"""
import base64
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from curl_cffi.requests import Cookies
from requests.cookies import create_cookie
from core import codex_oauth, protocol_registration
from core.cookie_scope import cookies_for_url
from core.session import BrowserSession


class CookieScopeReviewTests(unittest.TestCase):
    def session(self):
        return SimpleNamespace(session=SimpleNamespace(cookies=Cookies()))

    def add(self, session, domain, workspace, **kwargs):
        value = base64.urlsafe_b64encode(json.dumps(
            {'workspaces': [{'id': workspace}]}).encode()).decode().rstrip('=')
        cookie = create_cookie('oai-client-auth-session', value, domain=domain, **kwargs)
        session.session.cookies.jar.set_cookie(cookie)
        return cookie

    def test_workspace_ignores_cookies_not_visible_to_auth_root(self):
        for kind in ('other_host', 'child_host', 'host_only_parent', 'other_path', 'expired'):
            with self.subTest(kind=kind):
                session = self.session()
                domain = {'other_host': 'chatgpt.com', 'child_host': 'sub.auth.openai.com',
                          'host_only_parent': 'openai.com'}.get(kind, 'auth.openai.com')
                extra = {'path': '/workspace'} if kind == 'other_path' else {}
                if kind == 'expired':
                    extra['expires'] = int(time.time()) - 60
                cookie = self.add(session, domain, 'wrong', **extra)
                if kind == 'host_only_parent':
                    cookie.domain_specified = False
                self.assertEqual(protocol_registration.workspace_id(session), '')
                with patch.object(codex_oauth, '_WORKSPACE_COOKIE_WAIT_SECONDS', 0):
                    with self.assertRaises(RuntimeError):
                        codex_oauth._get_workspace_id(session)

    def test_workspace_uses_auth_cookie_even_if_other_host_was_inserted_first(self):
        session = self.session()
        self.add(session, 'chatgpt.com', 'wrong')
        self.add(session, 'auth.openai.com', 'correct')
        self.assertEqual(protocol_registration.workspace_id(session), 'correct')
        self.assertEqual(codex_oauth._get_workspace_id(session), 'correct')

    def test_parent_domain_cookie_remains_usable(self):
        session = self.session()
        self.add(session, '.openai.com', 'parent')
        self.assertEqual(protocol_registration.workspace_id(session), 'parent')
        self.assertEqual(codex_oauth._get_workspace_id(session), 'parent')

    def test_device_sync_ignores_expired_path_and_host_only_parent(self):
        for kind in ('expired', 'other_path', 'host_only_parent'):
            with self.subTest(kind=kind):
                session = object.__new__(BrowserSession)
                session.session = SimpleNamespace(cookies=Cookies())
                session.device_id = 'current'
                cookie = create_cookie('oai-did', 'stale', domain=(
                    'openai.com' if kind == 'host_only_parent' else 'auth.openai.com'),
                    path='/private' if kind == 'other_path' else '/',
                    expires=int(time.time()) - 60 if kind == 'expired' else None)
                if kind == 'host_only_parent':
                    cookie.domain_specified = False
                session.session.cookies.jar.set_cookie(cookie)
                self.assertEqual(session.sync_device_id_from_cookies('auth.openai.com'), 'current')

    def test_scope_path_boundary_secure_and_path_order(self):
        cookies = Cookies()
        for name, path, secure in (('root', '/', False), ('nested', '/private', False),
                                   ('tls', '/', True)):
            cookies.jar.set_cookie(create_cookie(name, 'v', domain='auth.openai.com',
                                                 path=path, secure=secure))
        self.assertEqual([c.name for c in cookies_for_url(
            cookies, 'https://auth.openai.com/private/child')], ['nested', 'root', 'tls'])
        self.assertEqual([c.name for c in cookies_for_url(
            cookies, 'http://auth.openai.com/private-other')], ['root'])

    def test_workspace_does_not_use_cookies_with_unknown_origin(self):
        session = self.session()
        self.add(session, '', 'unknown-origin')
        self.assertEqual(protocol_registration.workspace_id(session), '')
        with patch.object(codex_oauth, '_WORKSPACE_COOKIE_WAIT_SECONDS', 0):
            with self.assertRaises(RuntimeError):
                codex_oauth._get_workspace_id(session)
