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
from urllib.parse import urlparse


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

    def private_library(
        self,
        page_size: int = 100,
        max_pages: int = 100,
        *,
        require_complete: bool = True,
    ) -> dict[str, dict]:
        """Fetch the user's private JLC component library, indexed by C-code."""
        if not 1 <= page_size <= 100:
            raise JlcApiError("JLCPCB private-library page size must be between 1 and 100")
        if max_pages < 1:
            raise JlcApiError("JLCPCB private-library max pages must be at least 1")

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
                return result

        if require_complete:
            raise JlcApiError(
                "JLCPCB private-library response exceeded the pagination safety limit; "
                "stock was not changed"
            )
        return result

    def _post(self, path: str, payload: dict) -> dict:
        """POST a signed JSON request and return the decoded API response."""
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

        if not isinstance(data, dict):
            raise JlcApiError("JLCPCB API returned an invalid JSON response")
        code = data.get("code")
        if code not in (None, 0, "0", 200, "200"):
            message = data.get("message") or data.get("msg") or f"API code {code}"
            raise JlcApiError(f"JLCPCB API error: {message}")
        return data


SAFE_COMPONENT_FIELDS = (
    "componentCode",
    "componentModel",
    "componentSpecification",
    "componentBrandEn",
    "description",
    "firstTypeName",
    "secondTypeName",
    "stockCount",
    "componentLibraryType",
    "componentType",
    "packageType",
    "packageName",
    "dataManualUrl",
    "datasheetUrl",
)


def safe_component_detail(record: dict | None) -> dict:
    """Return catalogue facts that are safe to expose to an authenticated client.

    The response deliberately uses an allow-list and extracts only HTTP(S)
    image URLs. Unknown API fields remain visible by name for diagnostics, but
    their values are never returned.
    """
    record = record if isinstance(record, dict) else {}
    result = {
        key: record[key]
        for key in SAFE_COMPONENT_FIELDS
        if record.get(key) not in (None, "", [], {})
    }
    images: list[str] = []

    def collect(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                collect(child_value, str(child_key))
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child, key)
        elif "image" in key.casefold() and isinstance(value, str):
            parsed = urlparse(value)
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                images.append(value)

    collect(record)
    if images:
        result["imageUrls"] = list(dict.fromkeys(images))[:10]
    result["availableFields"] = sorted(str(key) for key in record)
    return result


def availability_summary(public: dict | None, private: dict | None) -> dict:
    """Normalize the relevant JLC stock buckets for display."""
    public = public or {}
    private = private or {}
    buckets = {
        "jlcpcb_parts": _number(private.get("jlcpcbParts")),
        "consigned": _number(private.get("consignedParts")),
    }
    public_stock = _number(public.get("stockCount"))
    standard_route_stock = public_stock + buckets["jlcpcb_parts"]
    consigned_route_stock = buckets["consigned"]
    usable_stock = max(standard_route_stock, consigned_route_stock)
    return {
        "public_stock": public_stock,
        "private_total": sum(buckets.values()),
        **buckets,
        "standard_route_stock": standard_route_stock,
        "consigned_route_stock": consigned_route_stock,
        "usable_stock": usable_stock,
        "preferred_route": (
            "consigned" if consigned_route_stock > standard_route_stock else "standard"
        ),
        # Keep this informational field visible to diagnostics, but do not
        # include it in managed stock or build-capacity calculations.
        "idle_stock": _number(private.get("idleStock")),
        "model": public.get("componentModel") or private.get("componentModel") or "",
        "specification": public.get("componentSpecification")
        or private.get("componentSpecification")
        or "",
        "brand": public.get("componentBrandEn") or private.get("componentBrandEn") or "",
    }


def private_stock_quantities(private: dict | None) -> dict[str, Decimal]:
    """Return the supported account inventory mirrored into InvenTree.

    Global Sourcing is deliberately excluded. Corrosion Instruments does not
    use that purchasing route; including it would also create an incompatible
    stock pool for JLCPCB assembly calculations.
    """
    private = private or {}
    return {
        "private": _stock_bucket_number(private.get("jlcpcbParts"), "jlcpcbParts"),
        "consigned": _stock_bucket_number(private.get("consignedParts"), "consignedParts"),
    }


def public_stock_quantity(public: dict | None) -> Decimal:
    """Return a strict public-catalogue quantity suitable for stock sync."""
    public = public or {}
    return _stock_bucket_number(public.get("stockCount"), "stockCount")


def _stock_bucket_number(value: Any, field_name: str) -> Decimal:
    """Strictly normalize a private-stock bucket before it can change stock."""
    if value in (None, ""):
        return Decimal("0")
    if isinstance(value, dict):
        for key in ("stockCount", "quantity", "available", "count", "total"):
            if key in value:
                return _stock_bucket_number(value[key], field_name)
        if not value:
            return Decimal("0")
        raise JlcApiError(f"JLCPCB returned an unsupported {field_name} value")
    if isinstance(value, list):
        return sum(
            (_stock_bucket_number(item, field_name) for item in value),
            Decimal("0"),
        )
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise JlcApiError(f"JLCPCB returned an invalid {field_name} quantity") from exc
    if not quantity.is_finite() or quantity < 0:
        raise JlcApiError(f"JLCPCB returned an invalid {field_name} quantity")
    return quantity


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
