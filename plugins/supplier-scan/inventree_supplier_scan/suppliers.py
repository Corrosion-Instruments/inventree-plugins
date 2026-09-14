"""Supplier product lookup helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import re
from typing import Any

import requests
from requests.compat import quote

from .barcodes import ParsedBarcode


class SupplierLookupError(RuntimeError):
    """Raised when supplier data cannot be loaded."""


@dataclass(frozen=True)
class SupplierPartSource:
    """Supplier-normalized part data."""

    supplier: str
    sku: str
    mpn: str
    name: str
    description: str
    manufacturer: str = ""
    supplier_link: str = ""
    datasheet_url: str = ""
    image_url: str = ""
    packaging: str = ""
    category_path: list[str] = field(default_factory=list)
    parameters: dict[str, str] = field(default_factory=dict)
    price_breaks: dict[int, tuple[Decimal, str]] = field(default_factory=dict)
    quantity_available: Decimal | None = None
    lookup_status: str = "barcode"
    lookup_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return {
            "supplier": self.supplier,
            "sku": self.sku,
            "mpn": self.mpn,
            "name": self.name,
            "description": self.description,
            "manufacturer": self.manufacturer,
            "supplier_link": self.supplier_link,
            "datasheet_url": self.datasheet_url,
            "image_url": self.image_url,
            "packaging": self.packaging,
            "category_path": self.category_path,
            "parameters": self.parameters,
            "price_breaks": {
                str(quantity): [str(price), currency]
                for quantity, (price, currency) in self.price_breaks.items()
            },
            "quantity_available": (
                str(self.quantity_available)
                if self.quantity_available is not None
                else None
            ),
            "lookup_status": self.lookup_status,
            "lookup_error": self.lookup_error,
        }


def lookup_source_part(
    parsed: ParsedBarcode,
    *,
    digikey_client_id: str = "",
    digikey_client_secret: str = "",
    digikey_currency: str = "AUD",
    digikey_language: str = "en",
    digikey_location: str = "AU",
    timeout: int = 15,
) -> SupplierPartSource:
    """Look up supplier data for a parsed barcode."""
    try:
        if parsed.supplier == "lcsc":
            return LCSCClient(timeout=timeout).fetch(parsed.sku, parsed)

        if parsed.supplier == "digikey" and digikey_client_id and digikey_client_secret:
            return DigiKeyClient(
                client_id=digikey_client_id,
                client_secret=digikey_client_secret,
                currency=digikey_currency,
                language=digikey_language,
                location=digikey_location,
                timeout=timeout,
            ).fetch(parsed.sku, parsed)

    except SupplierLookupError as exc:
        return lean_source(parsed, lookup_status="error", lookup_error=str(exc))

    return lean_source(parsed)


def lean_source(
    parsed: ParsedBarcode, *, lookup_status: str = "barcode", lookup_error: str = ""
) -> SupplierPartSource:
    """Build a minimal source from barcode data only."""
    name = parsed.mpn or parsed.sku
    description = f"{parsed.supplier_name} {parsed.sku}"
    if parsed.mpn:
        description = f"{description} / {parsed.mpn}"

    return SupplierPartSource(
        supplier=parsed.supplier,
        sku=parsed.sku,
        mpn=parsed.mpn,
        name=name,
        description=description,
        manufacturer=parsed.manufacturer,
        supplier_link=supplier_link_from_sku(parsed.supplier, parsed.sku),
        lookup_status=lookup_status,
        lookup_error=lookup_error,
    )


def supplier_link_from_sku(supplier: str, sku: str) -> str:
    """Return a basic supplier link for a SKU."""
    if supplier == "lcsc":
        return f"https://www.lcsc.com/product-detail/{quote(sku, safe='')}.html"
    if supplier == "digikey":
        return f"https://www.digikey.com/en/products/result?keywords={quote(sku, safe='')}"
    return ""


class LCSCClient:
    """Small wrapper around the public LCSC product endpoint."""

    PRODUCT_INFO_URL = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={}"

    def __init__(self, *, timeout: int = 15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Accept-Language": "en-US,en",
                "User-Agent": "Mozilla/5.0 InvenTreeSupplierScan/0.1",
            }
        )

    def fetch(self, sku: str, parsed: ParsedBarcode) -> SupplierPartSource:
        """Fetch product data from LCSC."""
        response = self.session.get(
            self.PRODUCT_INFO_URL.format(quote(sku, safe="")),
            timeout=self.timeout,
        )

        try:
            payload = response.json()
        except ValueError as exc:
            raise SupplierLookupError(f"LCSC returned invalid JSON: {exc}") from exc

        if response.status_code != 200 or payload.get("code") != 200:
            message = payload.get("msg") or response.reason or "unknown error"
            raise SupplierLookupError(f"LCSC lookup failed for {sku}: {message}")

        result = payload.get("result") or {}
        if not result:
            raise SupplierLookupError(f"LCSC did not return product data for {sku}")

        description = (
            result.get("productDescEn")
            or result.get("productIntroEn")
            or result.get("productNameEn")
            or parsed.mpn
            or sku
        )

        image_url = ""
        for candidate in reversed(result.get("productImages") or []):
            image_url = candidate
            if "front" in candidate:
                break

        price_breaks: dict[int, tuple[Decimal, str]] = {}
        for item in result.get("productPriceList") or []:
            ladder = safe_int(item.get("ladder"))
            price = safe_decimal(item.get("currencyPrice") or item.get("productPrice"))
            if ladder is not None and price is not None:
                price_breaks[ladder] = (
                    price,
                    currency_from_symbol(item.get("currencySymbol")) or "USD",
                )

        category_path = []
        for item in result.get("parentCatalogList") or []:
            if name := item.get("catalogNameEn"):
                category_path.append(name)
        if name := result.get("catalogName"):
            category_path.append(name)

        parameters = {
            item.get("paramNameEn"): item.get("paramValueEn")
            for item in result.get("paramVOList") or []
            if item.get("paramNameEn") and item.get("paramValueEn")
        }
        if package := result.get("encapStandard"):
            parameters.setdefault("Package Type", package)

        return SupplierPartSource(
            supplier="lcsc",
            sku=result.get("productCode") or sku,
            mpn=result.get("productModel") or parsed.mpn,
            name=result.get("title") or result.get("productNameEn") or parsed.mpn or sku,
            description=strip_html(description),
            manufacturer=strip_html(result.get("brandNameEn") or ""),
            supplier_link=lcsc_product_link(result),
            datasheet_url=result.get("pdfUrl") or "",
            image_url=image_url,
            packaging=strip_html(result.get("productArrange") or ""),
            category_path=dedupe(category_path),
            parameters=parameters,
            price_breaks=price_breaks,
            quantity_available=safe_decimal(result.get("stockNumber")),
            lookup_status="rich",
        )


class DigiKeyClient:
    """Small wrapper around DigiKey's official ProductInformation API."""

    BASE_URL = "https://api.digikey.com"
    TOKEN_URL = f"{BASE_URL}/v1/oauth2/token"
    PRODUCT_DETAILS_URL = f"{BASE_URL}/products/v4/search/{{}}/productdetails"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        currency: str,
        language: str,
        location: str,
        timeout: int = 15,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.currency = currency
        self.language = language
        self.location = location
        self.timeout = timeout
        self.session = requests.Session()

    def fetch(self, sku: str, parsed: ParsedBarcode) -> SupplierPartSource:
        """Fetch product data from DigiKey."""
        token = self.get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-DIGIKEY-Client-Id": self.client_id,
            "X-DIGIKEY-Locale-Language": self.language,
            "X-DIGIKEY-Locale-Currency": self.currency,
            "X-DIGIKEY-Locale-Site": self.location,
            "Accept": "application/json",
        }

        response = self.session.get(
            self.PRODUCT_DETAILS_URL.format(quote(sku, safe="")),
            headers=headers,
            timeout=self.timeout,
        )

        if response.status_code == 404:
            raise SupplierLookupError(f"DigiKey did not find {sku}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise SupplierLookupError(f"DigiKey returned invalid JSON: {exc}") from exc

        if response.status_code >= 400:
            message = payload.get("detail") or payload.get("title") or response.reason
            raise SupplierLookupError(f"DigiKey lookup failed for {sku}: {message}")

        product = payload.get("Product") or {}
        if not product:
            raise SupplierLookupError(f"DigiKey did not return product data for {sku}")

        variation = select_digikey_variation(product, sku)
        if not variation:
            raise SupplierLookupError(f"DigiKey did not return a product variation for {sku}")

        price_breaks = {}
        for item in variation.get("StandardPricing") or []:
            quantity = safe_int(item.get("BreakQuantity"))
            price = safe_decimal(item.get("UnitPrice"))
            if quantity is not None and price is not None:
                price_breaks[quantity] = (price, self.currency)

        category_path = digikey_category_path(product.get("Category") or {})
        parameters = {
            item.get("ParameterText"): item.get("ValueText")
            for item in product.get("Parameters") or []
            if item.get("ParameterText") and item.get("ValueText")
        }

        description = (
            (product.get("Description") or {}).get("DetailedDescription")
            or (product.get("Description") or {}).get("ProductDescription")
            or parsed.mpn
            or sku
        )

        manufacturer = (product.get("Manufacturer") or {}).get("Name") or parsed.manufacturer

        return SupplierPartSource(
            supplier="digikey",
            sku=variation.get("DigiKeyProductNumber") or sku,
            mpn=product.get("ManufacturerProductNumber") or parsed.mpn,
            name=product.get("ManufacturerProductNumber") or parsed.mpn or sku,
            description=strip_html(description),
            manufacturer=strip_html(manufacturer or ""),
            supplier_link=product.get("ProductUrl") or supplier_link_from_sku("digikey", sku),
            datasheet_url=product.get("DatasheetUrl") or "",
            image_url=product.get("PhotoUrl") or "",
            packaging=(variation.get("PackageType") or {}).get("Name") or "",
            category_path=category_path,
            parameters=parameters,
            price_breaks=price_breaks,
            quantity_available=safe_decimal(variation.get("QuantityAvailableforPackageType")),
            lookup_status="rich",
        )

    def get_token(self) -> str:
        """Request an OAuth2 access token."""
        response = self.session.post(
            self.TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
            timeout=self.timeout,
        )

        try:
            payload = response.json()
        except ValueError as exc:
            raise SupplierLookupError(f"DigiKey token endpoint returned invalid JSON: {exc}") from exc

        if response.status_code >= 400:
            message = payload.get("error_description") or payload.get("error") or response.reason
            raise SupplierLookupError(f"DigiKey token request failed: {message}")

        token = payload.get("access_token")
        if not token:
            raise SupplierLookupError("DigiKey token response did not include access_token")

        return token


def select_digikey_variation(product: dict[str, Any], sku: str) -> dict[str, Any] | None:
    """Select the matching or smallest MOQ DigiKey variation."""
    variations = product.get("ProductVariations") or []
    for variation in variations:
        if variation.get("DigiKeyProductNumber") == sku:
            return variation

    if not variations:
        return None

    return sorted(
        variations,
        key=lambda item: item.get("MinimumOrderQuantity") or 0,
    )[0]


def digikey_category_path(category: dict[str, Any]) -> list[str]:
    """Flatten DigiKey's nested category structure."""
    path = []
    current = category
    while current:
        if name := current.get("Name"):
            path.append(name)
        children = current.get("ChildCategories") or []
        current = children[0] if children else None
    return path


def lcsc_product_link(result: dict[str, Any]) -> str:
    """Build an LCSC product link."""
    if url := result.get("url"):
        return url

    category = cleanup_link_text(result.get("catalogName") or "product")
    title = cleanup_link_text(result.get("title") or result.get("productModel") or "part")
    sku = quote(result.get("productCode") or "", safe="")
    return f"https://www.lcsc.com/product-detail/{category}_{title}_{sku}.html"


def cleanup_link_text(value: str) -> str:
    """Format a URL slug component."""
    value = value.replace(" / ", "_").replace("/", "_")
    value = re.sub(r"[^\w\d.]+", "-", value)
    return re.sub(r"-{2,}", "-", value).strip("-")


def strip_html(value: Any) -> str:
    """Remove HTML tags from supplier strings."""
    text = str(value or "")
    return re.sub(r"<[^>]+>", "", text).strip()


def currency_from_symbol(symbol: str | None) -> str:
    """Map common currency symbols to ISO codes."""
    return {
        "$": "USD",
        "US$": "USD",
        "AUD": "AUD",
        "A$": "AUD",
        "€": "EUR",
        "£": "GBP",
        "¥": "CNY",
        "HK$": "HKD",
    }.get(symbol or "", "")


def safe_decimal(value: Any) -> Decimal | None:
    """Convert a value to Decimal if possible."""
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def safe_int(value: Any) -> int | None:
    """Convert a value to int if possible."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        return None


def dedupe(values: list[str]) -> list[str]:
    """Deduplicate while preserving order."""
    seen = set()
    output = []
    for value in values:
        value = value.strip()
        if value and value not in seen:
            output.append(value)
            seen.add(value)
    return output
