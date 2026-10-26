"""身份读取使用的 URL 作用域快照，不改变原生 HTTP Cookie Jar。"""
from urllib.parse import urlparse


def cookies_for_url(cookies, url):
    """统一 curl_cffi/requests 与 wreq 的域、路径、有效期筛选。"""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    request_path = parsed.path or "/"
    scoped = getattr(cookies, "jar_for_url", None)
    source = scoped(url) if callable(scoped) else getattr(cookies, "jar", cookies)
    result = []
    for cookie in source:
        domain = (cookie.domain or "").lstrip(".").lower()
        # 无来源域不能用于确定身份；wreq 的 scoped 快照会补回来源域。
        if not domain or (host != domain and not (
                cookie.domain_specified and host.endswith("." + domain))):
            continue
        path = cookie.path or "/"
        if request_path != path and not (request_path.startswith(path) and (
                path.endswith("/") or request_path[len(path):].startswith("/"))):
            continue
        if cookie.is_expired() or (cookie.secure and parsed.scheme != "https"):
            continue
        result.append(cookie)
    # 浏览器按路径长度排列同名 Cookie；相同路径保持 Jar 原有顺序。
    return sorted(result, key=lambda cookie: -len(cookie.path or "/"))
