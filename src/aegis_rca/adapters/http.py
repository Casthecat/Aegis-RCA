"""Bounded read-only HTTP transport. Exceptions never include payloads or URLs."""
from __future__ import annotations

import json as json_module
import math
import time
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx


class AdapterError(RuntimeError):
    """Sanitized transport/backend failure suitable for an investigation audit."""


class SchemaError(AdapterError):
    """An incomplete or malformed response must not become negative evidence."""


class JsonHttp:
    def __init__(self, base_url: str, *, timeout_seconds: float = 10,
                 max_response_bytes: int = 4_000_000,
                 transport: httpx.BaseTransport | None = None,
                 headers: dict[str, str] | None = None):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("base_url must be an HTTP(S) URL without credentials, query, or fragment")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be in (0, 60]")
        if not 1 <= max_response_bytes <= 64_000_000:
            raise ValueError("max_response_bytes must be in [1, 64000000]")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/", timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False, trust_env=False, transport=transport,
            headers={"Accept": "application/json", "Accept-Encoding": "identity", **(headers or {})},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> JsonHttp:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _read(self, method: str, path: str, *, params: dict | None = None,
              json: Any = None) -> str:
        decoded = unquote(path)
        if (not path.startswith("/") or path.startswith("//") or "\\" in decoded
                or any(part in {".", ".."} for part in decoded.split("/"))
                or "?" in decoded or "#" in decoded or ":" in decoded):
            raise ValueError("request path must be a local absolute path without query or traversal")
        if method != "GET" and not (method == "POST" and path.endswith("/_search")):
            raise ValueError("only GET and POST search requests are supported")
        started = time.monotonic()
        content = bytearray()
        try:
            with self._client.stream(method, path.lstrip("/"), params=params, json=json) as response:
                if not 200 <= response.status_code < 300:
                    raise AdapterError(f"backend HTTP status {response.status_code}")
                # iter_bytes bounds decoded content, including a server ignoring identity encoding.
                for chunk in response.iter_bytes():
                    if time.monotonic() - started > self.timeout_seconds:
                        raise AdapterError("backend response exceeded time budget")
                    if len(content) + len(chunk) > self.max_response_bytes:
                        raise AdapterError("backend response exceeded byte budget")
                    content.extend(chunk)
        except httpx.TimeoutException:
            raise AdapterError("backend request timed out") from None
        except httpx.HTTPError:
            raise AdapterError("backend transport failed") from None
        try:
            return content.decode("utf-8")
        except UnicodeError:
            raise SchemaError("backend response is not UTF-8 JSON") from None

    def request(self, method: str, path: str, *, params: dict | None = None,
                json: Any = None) -> Any:
        raw = self._read(method, path, params=params, json=json)
        try:
            return json_module.loads(raw)
        except (ValueError, RecursionError):
            raise SchemaError("backend response is not one valid JSON document") from None

    def stream_documents(self, path: str, *, params: dict | None = None) -> tuple[dict, ...]:
        """Decode grpc-gateway's consecutive JSON envelopes; fail on a partial tail."""
        raw = self._read("GET", path, params=params)
        documents: list[dict] = []
        decoder = json_module.JSONDecoder()
        pos = 0
        try:
            while pos < len(raw):
                while pos < len(raw) and raw[pos].isspace():
                    pos += 1
                if pos == len(raw):
                    break
                value, pos = decoder.raw_decode(raw, pos)
                if not isinstance(value, dict):
                    raise SchemaError("Jaeger stream envelope must be an object")
                documents.append(value)
        except (ValueError, RecursionError):
            raise SchemaError("Jaeger stream contains malformed or incomplete JSON") from None
        return tuple(documents)
