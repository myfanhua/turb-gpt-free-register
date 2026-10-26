"""将 wreq 的同步客户端适配为 BrowserSession 使用的 requests 风格接口。"""
from datetime import timedelta
from http.client import responses
from collections.abc import Mapping
from urllib.parse import urlparse

from requests import Response, exceptions
from requests.cookies import RequestsCookieJar, create_cookie
from requests.structures import CaseInsensitiveDict
from requests.utils import get_encoding_from_headers


class WreqCookies:
    def __init__(self, native, wreq):
        self.native = native
        self._wreq = wreq

    def set(self, name, value, domain="", path="/", secure=False, **kwargs):
        if not domain:
            raise ValueError("wreq Cookie 必须指定 domain")
        origin = f"{'https' if secure else 'http'}://{domain.lstrip('.')}{path}"
        self.native.add(self._wreq.Cookie(
            name, str(value), domain=domain, path=path, secure=secure, **kwargs
        ), origin)

    @staticmethod
    def _snapshot(cookies, *, host="", path="/"):
        jar = RequestsCookieJar()
        for cookie in cookies:
            item = create_cookie(
                cookie.name, cookie.value, domain=cookie.domain or host,
                path=cookie.path or path, secure=cookie.secure,
                expires=int(cookie.expires.timestamp()) if cookie.expires else None,
                rest={"HttpOnly": None} if cookie.http_only else {},
            )
            if cookie.domain is None:
                item.domain_specified = False
            jar.set_cookie(item)
        return jar

    @property
    def jar(self):
        # get_all 对 host-only Cookie 不返回来源域名；此快照只用于无域枚举。
        return self._snapshot(self.native.get_all())

    def jar_for_url(self, url):
        """按URL重建快照。Jar.get是域/路径键查找，并非HTTP可见性筛选。"""
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        request_path = parsed.path or "/"
        # 原生get_all没有host-only来源域；仅向目标主机查询，禁止全域复制。
        # 未显式Path的Cookie也没有default-path元数据，按请求路径祖先探测其存储键。
        ancestors = {"/", request_path}
        for index, char in enumerate(request_path):
            if char == "/":
                ancestors.update((request_path[:index] or "/", request_path[:index + 1]))
        lookups = set()
        for cookie in self.native.get_all():
            domain = (cookie.domain or host).lstrip(".").lower()
            if domain != host and not host.endswith("." + domain):
                continue
            paths = [cookie.path] if cookie.path else ancestors
            for path in paths:
                if request_path == path or (request_path.startswith(path)
                        and (path.endswith("/") or request_path[len(path):].startswith("/"))):
                    lookups.add((cookie.name, domain, path))
        jar = RequestsCookieJar()
        for name, domain, path in sorted(lookups, key=lambda item: (-len(item[2]), item)):
            netloc = f"[{domain}]" if ":" in domain else domain
            cookie = self.native.get(name, f"{parsed.scheme}://{netloc}{path}")
            if cookie is None or (cookie.secure and parsed.scheme != "https"):
                continue
            if cookie.domain is None and domain != host:
                continue
            for item in self._snapshot([cookie], host=domain, path=path):
                if not item.is_expired():
                    jar.set_cookie(item)
        return jar

    def get(self, name, default=None, domain=None, path=None):
        return self.jar.get(name, default, domain=domain, path=path)

    def clear(self):
        self.native.clear()


class WreqSession:
    def __init__(self, impersonate):
        try:
            import wreq
            from wreq.blocking import Client
        except ImportError as exc:
            raise RuntimeError("使用 wreq 需要 Python >=3.11，并安装 requirements.txt 中的 wreq") from exc
        profile_name = next((name for name in dir(wreq.Profile)
                             if name.lower() == impersonate.lower()), "")
        profile = getattr(wreq.Profile, profile_name, None)
        if profile is None:
            raise ValueError(f"wreq 不支持指纹 {impersonate}，请检查浏览器配置及 wreq 版本")
        self.impersonate = profile_name
        self._wreq = wreq
        jar = wreq.Jar()
        self._client = Client(emulation=profile, cookie_provider=jar,
                              default_headers=False, no_proxy=True)
        self.cookies = WreqCookies(jar, wreq)
        self.proxies = {}
        self.timeout = 30

    def request(self, method, url, **kwargs):
        if self._client is None:
            raise RuntimeError("wreq 会话已关闭")
        options = {}
        for key in ("headers", "json", "cookies"):
            if key in kwargs:
                options[key] = kwargs.pop(key)
        if "params" in kwargs:
            options["query"] = kwargs.pop("params")
        if "data" in kwargs:
            data = kwargs.pop("data")
            options["form" if isinstance(data, (Mapping, list, tuple)) else "body"] = data
        timeout = kwargs.pop("timeout", self.timeout)
        if timeout is not None:
            options["timeout"] = timedelta(seconds=float(timeout))
        options["redirect"] = (self._wreq.redirect.Policy.limited(30)
                               if kwargs.pop("allow_redirects", True)
                               else self._wreq.redirect.Policy.none())
        proxies = kwargs.pop("proxies", self.proxies) or {}
        proxy = proxies.get(urlparse(url).scheme) or proxies.get("all")
        if proxy:
            options["proxy"] = self._wreq.Proxy.all(proxy)
        if kwargs:
            raise TypeError(f"wreq 暂不支持请求参数: {', '.join(sorted(kwargs))}")
        try:
            with self._client.request(getattr(self._wreq.Method, method.upper()), url, **options) as raw:
                response = Response()
                response.status_code = raw.status.as_int()
                response.reason = responses.get(response.status_code, "")
                response.url = str(raw.url)
                response.headers = CaseInsensitiveDict((bytes(k).decode("ascii"), bytes(v).decode("latin-1"))
                                                       for k, v in raw.headers)
                response._content = bytes(raw.bytes())
                response._content_consumed = True
                response.encoding = get_encoding_from_headers(response.headers)
                response.cookies = self.cookies.jar_for_url(response.url)
                return response
        except self._wreq.TimeoutError as exc:
            raise exceptions.Timeout(str(exc)) from exc
        except self._wreq.RequestError as exc:
            raise exceptions.ConnectionError(str(exc)) from exc

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def close(self):
        # blocking.Client 通过上下文管理器释放连接池。
        if self._client is not None:
            self._client.__exit__(None, None, None)
            self._client = None
