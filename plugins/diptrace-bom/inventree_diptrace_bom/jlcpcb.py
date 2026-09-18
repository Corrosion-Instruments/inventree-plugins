"""Authenticated client for the official JLCPCB Open API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


class JlcApiError(RuntimeError):
    """Raised when the JLCPCB Open API request fails."""


@dataclass(frozen=True)
class JlcCredentials:
    app_id: str
    access_key: str
    tokenization_key: str

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.access_key and self.tokenization_key)


def compact_json(payload: dict) -> str:
    """Serialize a request body in the form used for JLC request signing."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def make_signature(
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: str,
    tokenization_key: str,
) -> str:
    """Return the Base64 HMAC-SHA256 signature required by JLCPCB."""
    canonical = f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body}\n"
    digest = hmac.new(
        tokenization_key.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


class JlcClient:
    """Small server-side client for public and private JLC part availability."""

    DETAIL_PATH = "/overseas/openapi/component/getComponentDetailByCode"
    PRIVATE_PATH = "/overseas/openapi/component/getPrivateComponentLibrary"

    def __init__(
        self,
        credentials: JlcCredentials,
        *,
        host: str = "https://open.jlcpcb.com",
        timeout: int = 30,
        session: Any | None = None,
    ):
        if not credentials.configured:
            raise JlcApiError("JLCPCB API credentials are not configured in plugin settings")
        self.credentials = credentials
        self.host = host.rstrip("/")
        self.timeout = timeout
        if session is None:
            import requests

            session = requests.Session()
        self.session = session

    def component_details(self, component_codes: list[str]) -> dict[str, dict]:
        """Fetch public catalogue details and stock for up to 1,000 C-codes."""
        codes = list(dict.fromkeys(code for code in component_codes if code))
        result: dict[str, dict] = {}
        for index in range(0, len(codes), 1000):
            payload = self._post(self.DETAIL_PATH, {"componentCodes": codes[index : index + 1000]})
            for item in _component_rows(payload):
                code = str(item.get("componentCode") or item.get("component_code") or "").upper()
                if code:
                    result[code] = item
        return result

    def private_library(self, page_size: int = 1000, max_pages: int = 100) -> dict[str, dict]:
        """Fetch the user's private JLC component library, indexed by C-code."""
        result: dict[str, dict] = {}
        for page in range(1, max_pages + 1):
            payload = self._post(
                self.PRIVATE_PATH,
                {"currentPage": page, "pageSize": page_size},
            )
            rows = _component_rows(payload)
            for item in rows:
                code = str(item.get("componentCode") or item.get("component_code") or "").upper()
                if code:
                    result[code] = item
            if len(rows) < page_size:
                break
        return result

    def _post(self, path: str, payload: dict) -> dict:
        import requests

        body = compact_json(payload)
        # JLCPCB signs a Unix timestamp in seconds (not milliseconds).
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        signature = make_signature(
            "POST",
            path,
            timestamp,
            nonce,
            body,
            self.credentials.tokenization_key,
        )
        authorization = (
            f'JOP appid="{self.credentials.app_id}",'
            f'accesskey="{self.credentials.access_key}",'
            f'nonce="{nonce}",timestamp="{timestamp}",signature="{signature}"'
        )
        try:
            response = self.session.post(
                f"{self.host}{path}",
                data=body.encode("utf-8"),
                headers={
                    "Authorization": authorization,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise JlcApiError(f"JLCPCB API request failed: {exc}") from exc

        code = data.get("code")
        if code not in (None, 0, "0", 200, "200"):
            message = data.get("message") or data.get("msg") or f"API code {code}"
            raise JlcApiError(f"JLCPCB API error: {message}")
        return data


def availability_summary(public: dict | None, private: dict | None) -> dict:
    """Normalize the relevant JLC stock buckets for display."""
    public = public or {}
    private = private or {}
    buckets = {
        "jlcpcb_parts": _number(private.get("jlcpcbParts")),
        "global_sourcing": _number(private.get("globalSourcingParts")),
        "consigned": _number(private.get("consignedParts")),
        "idle_stock": _number(private.get("idleStock")),
    }
    return {
        "public_stock": _number(public.get("stockCount")),
        "private_total": sum(buckets.values()),
        **buckets,
        "model": public.get("componentModel") or private.get("componentModel") or "",
        "specification": public.get("componentSpecification")
        or private.get("componentSpecification")
        or "",
        "brand": public.get("componentBrandEn") or private.get("componentBrandEn") or "",
    }


def _component_rows(payload: Any) -> list[dict]:
    """Find the component list in the documented response envelope."""
    preferred_keys = (
        "componentDetailResponseVOList",
        "privateComponentLibraryInfoVOS",
        "componentLibraryInfoVOS",
        "componentInfos",
        "componentInfoList",
        "componentLibraryList",
        "list",
        "records",
        "items",
        "rows",
    )
    queue = [payload]
    visited = set()
    while queue:
        candidate = queue.pop(0)
        marker = id(candidate)
        if marker in visited:
            continue
        visited.add(marker)
        if isinstance(candidate, list):
            rows = [row for row in candidate if isinstance(row, dict)]
            if any("componentCode" in row or "component_code" in row for row in rows):
                return rows
        elif isinstance(candidate, dict):
            for key in preferred_keys:
                if key in candidate:
                    queue.insert(0, candidate[key])
            queue.extend(candidate.values())
    return []


def _number(value) -> Decimal:
    if isinstance(value, dict):
        for key in ("stockCount", "quantity", "available", "count", "total"):
            if key in value:
                return _number(value[key])
        return Decimal("0")
    if isinstance(value, list):
        return sum((_number(item) for item in value), Decimal("0"))
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal("0")
