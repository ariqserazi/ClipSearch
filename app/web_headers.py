import os
from typing import Any


DEFAULT_HEADERS = {
    "User-Agent": os.getenv(
        "USER_AGENT",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}


def outbound_proxy_url() -> str | None:
    for name in ("OUTBOUND_PROXY_URL", "HTTPS_PROXY", "HTTP_PROXY"):
        value = os.getenv(name)
        if value:
            return value
    return None


def web_client_kwargs(*, timeout: float = 30.0, follow_redirects: bool = True) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "follow_redirects": follow_redirects,
        "headers": DEFAULT_HEADERS,
    }
    proxy = outbound_proxy_url()
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs
