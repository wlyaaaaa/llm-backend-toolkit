"""Transport policy for exact provider destinations; no global urllib changes."""
from __future__ import annotations

import ipaddress
import urllib.parse
import urllib.request

from .errors import ProviderCallError, ToolError


def normalize_endpoint(value: str) -> tuple[str, bool]:
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Provider endpoint must be a non-empty HTTP(S) URL")
    try:
        url = urllib.parse.urlsplit(value)
        host, port = url.hostname, url.port
    except ValueError:
        raise ValueError("Provider endpoint is malformed") from None
    if (url.scheme not in {"http", "https"} or not host or url.username is not None
            or url.password is not None or url.query or url.fragment or "\\" in value):
        raise ValueError("Provider endpoint must use HTTP(S), without credentials, query or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Provider endpoint port is invalid")
    host = host.lower()
    if host in {"localhost", "localhost."}:
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        local = False
    else:
        local = address.is_loopback
        host = address.compressed
        if address.version == 6:
            host = "[" + host + "]"
    authority = host + (f":{port}" if port and port != (443 if url.scheme == "https" else 80) else "")
    path = url.path.rstrip("/")
    return urllib.parse.urlunsplit((url.scheme, authority, path, "", "")), local


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderCallError(ToolError(
            category="provider_redirect_blocked",
            summary="Provider redirected the request; no redirected request was sent.",
            retryable=False, options=("inspect-provider-endpoint",),
        ))


def open_response(request: urllib.request.Request, timeout: int):
    _, local = normalize_endpoint(request.full_url)
    handlers = [RejectRedirects()]
    if local:
        # Local material must not pass through an inherited HTTP proxy.
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers).open(request, timeout=timeout)
