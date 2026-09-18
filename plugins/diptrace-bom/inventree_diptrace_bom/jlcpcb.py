"""Authenticated client for the official JLCPCB Open API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
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

    def private_library(self, page_size: int = 100, max_pages: int = 100) -> dict[str, dict]:
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

    def diagnose_private_library(
        self,
        component_code: str,
        *,
        page_size: int = 100,
        max_pages: int = 100,
    ) -> dict:
        """Fetch private inventory with a credential-safe response-shape report."""
        library: dict[str, dict] = {}
        page_reports: list[dict] = []
        for page in range(1, max_pages + 1):
            payload, transport = self._post_with_transport(
                self.PRIVATE_PATH,
                {"currentPage": page, "pageSize": page_size},
            )
            rows = _component_rows(payload)
            for item in rows:
                code = str(item.get("componentCode") or item.get("component_code") or "").upper()
                if code:
                    library[code] = item

            shape = response_shape_summary(payload)
            page_reports.append(
                {
                    "requested_page": page,
                    "requested_page_size": page_size,
                    "call_time_utc": transport["call_time_utc"],
                    "app_id": self.credentials.app_id,
                    "interface": self.PRIVATE_PATH,
                    "http_status": transport["http_status"],
                    "content_type": transport["content_type"],
                    "api_code": _safe_api_code(payload),
                    "api_message": _safe_api_message(payload),
                    "j_trace_id": transport["j_trace_id"],
                    "extracted_rows": len(rows),
                    "identified_components": sum(
                        1
                        for item in rows
                        if item.get("componentCode") or item.get("component_code")
                    ),
                    **shape,
                }
            )
            if len(rows) < page_size:
                break

        result = private_inventory_diagnostic(component_code, library)
        result["pages_requested"] = len(page_reports)
        result["response_pages"] = page_reports
        return result

    def _post(self, path: str, payload: dict) -> dict:
        data, _transport = self._post_with_transport(path, payload)
        return data

    def _post_with_transport(self, path: str, payload: dict) -> tuple[dict, dict]:
        """POST to JLCPCB and return JSON plus non-sensitive transport metadata."""
        import requests

        body = compact_json(payload)
        # JLCPCB signs a Unix timestamp in seconds (not milliseconds).
        timestamp = str(int(time.time()))
        call_time_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(timestamp)))
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
        return data, {
            "call_time_utc": call_time_utc,
            "http_status": response.status_code,
            "content_type": str(response.headers.get("Content-Type") or "").split(";", 1)[0],
            "j_trace_id": str(response.headers.get("J-Trace-ID") or "").strip(),
        }


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


def private_inventory_diagnostic(component_code: str, library: dict[str, dict]) -> dict:
    """Return a credential-safe diagnostic for one private-library component."""
    code = str(component_code or "").strip().upper()
    item = library.get(code)
    diagnostic = {
        "component_code": code,
        "found": item is not None,
        "library_entries_scanned": len(library),
        "returned_field_names": sorted(str(key) for key in (item or {}).keys()),
        "inventory": {
            "jlcpcb_parts": "0",
            "global_sourcing": "0",
            "consigned": "0",
            "idle_stock": "0",
            "private_total": "0",
        },
    }
    if item is not None:
        summary = availability_summary(None, item)
        diagnostic["inventory"] = {
            key: str(summary[key])
            for key in (
                "jlcpcb_parts",
                "global_sourcing",
                "consigned",
                "idle_stock",
                "private_total",
            )
        }
    return diagnostic


_SENSITIVE_FIELD_MARKERS = (
    "accesskey",
    "access_key",
    "appid",
    "app_id",
    "authorization",
    "credential",
    "nonce",
    "secret",
    "signature",
    "token",
)

_PAGINATION_FIELDS = {
    "currentpage",
    "page",
    "pagecount",
    "pagenum",
    "pagenumber",
    "pages",
    "pagesize",
    "recordcount",
    "records",
    "recordstotal",
    "total",
    "totalcount",
    "totalpages",
}


def response_shape_summary(payload: Any, *, max_depth: int = 4, max_nodes: int = 40) -> dict:
    """Describe a JSON response without returning any response values or credentials."""
    containers: list[dict] = []
    pagination: dict[str, str] = {}
    queue: list[tuple[str, Any, int]] = [("$", payload, 0)]

    while queue and len(containers) < max_nodes:
        path, candidate, depth = queue.pop(0)
        if isinstance(candidate, dict):
            safe_keys = sorted(
                str(key) for key in candidate if not _is_sensitive_field(str(key))
            )
            containers.append(
                {
                    "path": path,
                    "type": "object",
                    "field_names": safe_keys,
                    "redacted_field_count": len(candidate) - len(safe_keys),
                }
            )
            for key, value in candidate.items():
                key_text = str(key)
                if _is_sensitive_field(key_text):
                    continue
                normalized = re.sub(r"[^a-z0-9]", "", key_text.lower())
                if normalized in _PAGINATION_FIELDS and _is_safe_scalar(value):
                    pagination[f"{path}.{key_text}"] = str(value)
                if depth < max_depth and isinstance(value, (dict, list)):
                    queue.append((f"{path}.{key_text}", value, depth + 1))
        elif isinstance(candidate, list):
            report = {"path": path, "type": "list", "length": len(candidate)}
            first_mapping = next((item for item in candidate if isinstance(item, dict)), None)
            if first_mapping is not None:
                report["item_field_names"] = sorted(
                    str(key)
                    for key in first_mapping
                    if not _is_sensitive_field(str(key))
                )
                report["redacted_item_field_count"] = sum(
                    1 for key in first_mapping if _is_sensitive_field(str(key))
                )
                if depth < max_depth:
                    queue.append((f"{path}[0]", first_mapping, depth + 1))
            containers.append(report)

    return {
        "response_containers": containers,
        "pagination": pagination,
        "shape_truncated": bool(queue),
    }


def _is_sensitive_field(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9_]", "", name.lower())
    return any(marker in normalized for marker in _SENSITIVE_FIELD_MARKERS)


def _is_safe_scalar(value: Any) -> bool:
    return isinstance(value, (int, float)) or (
        isinstance(value, str) and bool(re.fullmatch(r"-?\d+(?:\.\d+)?", value.strip()))
    )


def _safe_api_code(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "not present"
    value = payload.get("code")
    return str(value) if _is_safe_scalar(value) else "not present"


def _safe_api_message(payload: Any) -> str:
    """Return only the top-level API message, capped for safe display."""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("message")
    if value is None:
        value = payload.get("msg")
    if not isinstance(value, (str, int, float, bool)):
        return ""
    return str(value)[:500]


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
