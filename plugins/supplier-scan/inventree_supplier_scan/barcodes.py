"""Supplier barcode parsing helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import re
from typing import Any


class BarcodeParseError(ValueError):
    """Raised when a barcode cannot be parsed by this plugin."""


@dataclass(frozen=True)
class ParsedBarcode:
    """Normalized supplier barcode information."""

    supplier: str
    supplier_name: str
    sku: str
    quantity: Decimal | None = None
    mpn: str = ""
    manufacturer: str = ""
    order_number: str = ""
    lot_code: str = ""
    date_code: str = ""
    raw_fields: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return {
            "supplier": self.supplier,
            "supplier_name": self.supplier_name,
            "sku": self.sku,
            "quantity": str(self.quantity) if self.quantity is not None else None,
            "mpn": self.mpn,
            "manufacturer": self.manufacturer,
            "order_number": self.order_number,
            "lot_code": self.lot_code,
            "date_code": self.date_code,
            "raw_fields": self.raw_fields,
        }


LCSC_PATTERN = re.compile(r"^\{((?:[^:,]+:[^:,]*,)*(?:[^:,]+:[^:,]*))\}$")

DIGIKEY_HEADER_STANDARD = "[)>\x1e06\x1d"
DIGIKEY_HEADER_LEGACY = ">[)>06\x1d"
DIGIKEY_TRAILER = "\x1e\x04"
DIGIKEY_SEPARATOR = "\x1d"

DIGIKEY_FIELD_MAP = {
    "P": "supplier_part_number",
    "30P": "supplier_part_number",
    "1P": "manufacturer_part_number",
    "Q": "quantity",
    "1K": "supplier_order_number",
    "K": "customer_order_number",
    "9D": "date_code",
    "10K": "supplier_order_number_alt",
    "1T": "lot_code",
    "4L": "country_of_origin",
    "1V": "manufacturer",
}

DIGIKEY_IDENTIFIERS = sorted(DIGIKEY_FIELD_MAP, key=len, reverse=True)
DIGIKEY_FLAT_END_TOKENS = [
    "30P",
    "11K",
    "14L",
    "11Z",
    "12Z",
    "13Z",
    "20Z",
    "10K",
    "1K",
    "9D",
    "1T",
    "4L",
    "1P",
    "Q",
    "K",
]
DIGIKEY_SEPARATOR_MARKER_PATTERN = re.compile(
    rf"q(?=(?:{'|'.join(re.escape(token) for token in DIGIKEY_FLAT_END_TOKENS)}|P))"
)


def parse_barcode(barcode: str) -> ParsedBarcode:
    """Parse a supported supplier barcode."""
    normalized = normalize_barcode(barcode)

    if parsed := parse_lcsc_barcode(normalized):
        return parsed

    if parsed := parse_digikey_barcode(normalized):
        return parsed

    raise BarcodeParseError("Unsupported supplier barcode format")


def normalize_barcode(barcode: str) -> str:
    """Normalize common keyboard-wedge scanner substitutions."""
    barcode = str(barcode or "").strip()

    # Some scanner layouts emit ISO/IEC 15434 group separators as these glyphs.
    barcode = barcode.replace("\u00ed", DIGIKEY_SEPARATOR)
    barcode = barcode.replace("\u001d", DIGIKEY_SEPARATOR)

    # Other keyboard-wedge profiles substitute the GS control character with a
    # lowercase q. DigiKey payloads are uppercase, so only treat q as a
    # separator when it is immediately followed by a known ECIA field token.
    if barcode.startswith(("[)>06", ">[)>06")):
        barcode = DIGIKEY_SEPARATOR_MARKER_PATTERN.sub(DIGIKEY_SEPARATOR, barcode)

    # Some scanner profiles drop RS/GS separators from the header.
    if barcode.startswith("[)>06") and not barcode.startswith(DIGIKEY_HEADER_STANDARD):
        barcode = DIGIKEY_HEADER_STANDARD + barcode[len("[)>06") :]

    if barcode.startswith(">[)>06") and not barcode.startswith(DIGIKEY_HEADER_LEGACY):
        barcode = DIGIKEY_HEADER_LEGACY + barcode[len(">[)>06") :]

    return barcode


def parse_lcsc_barcode(barcode: str) -> ParsedBarcode | None:
    """Parse LCSC/JLC QR data."""
    if not LCSC_PATTERN.fullmatch(barcode):
        return None

    raw_fields = {}
    for item in barcode.strip("{}").split(","):
        key, value = item.split(":", 1)
        raw_fields[key] = value

    sku = raw_fields.get("pc", "").strip()
    if not sku:
        return None

    return ParsedBarcode(
        supplier="lcsc",
        supplier_name="LCSC",
        sku=sku,
        mpn=(raw_fields.get("pm") or raw_fields.get("mc") or "").strip(),
        quantity=parse_decimal(raw_fields.get("qty")),
        order_number=(raw_fields.get("on") or "").strip(),
        raw_fields=raw_fields,
    )


def parse_digikey_barcode(barcode: str) -> ParsedBarcode | None:
    """Parse DigiKey ECIA 2D barcode data."""
    fields = parse_digikey_structured_fields(barcode)

    flat_fields = parse_digikey_flat_fields(barcode)

    # Some scanner profiles preserve only one separator. In that case the
    # structured parser can recover the SKU but treat the rest as one long MPN.
    if not fields:
        fields = flat_fields
    elif flat_fields and (
        not fields.get("quantity")
        or "30P" in fields.get("manufacturer_part_number", "")
        or "10K" in fields.get("manufacturer_part_number", "")
    ):
        fields.update({key: value for key, value in flat_fields.items() if value})

    sku = fields.get("supplier_part_number", "").strip()
    if not sku:
        return None

    return ParsedBarcode(
        supplier="digikey",
        supplier_name="DigiKey",
        sku=sku,
        mpn=fields.get("manufacturer_part_number", "").strip(),
        manufacturer=fields.get("manufacturer", "").strip(),
        quantity=parse_decimal(fields.get("quantity")),
        order_number=(
            fields.get("supplier_order_number")
            or fields.get("supplier_order_number_alt")
            or fields.get("customer_order_number")
            or ""
        ).strip(),
        lot_code=fields.get("lot_code", "").strip(),
        date_code=fields.get("date_code", "").strip(),
        raw_fields=fields,
    )


def parse_digikey_structured_fields(barcode: str) -> dict[str, str]:
    """Parse ECIA fields when separators are present."""
    if barcode.startswith(DIGIKEY_HEADER_LEGACY):
        barcode = barcode.replace(DIGIKEY_HEADER_LEGACY, DIGIKEY_HEADER_STANDARD, 1)

    if not barcode.startswith(DIGIKEY_HEADER_STANDARD):
        return {}

    data = barcode[len(DIGIKEY_HEADER_STANDARD) :]
    if data.endswith(DIGIKEY_TRAILER):
        data = data[: -len(DIGIKEY_TRAILER)]

    output = {}
    for field_data in data.split(DIGIKEY_SEPARATOR):
        if not field_data:
            continue
        key, value = split_digikey_field(field_data)
        if key:
            output[DIGIKEY_FIELD_MAP[key]] = value

    return output


def parse_digikey_flat_fields(barcode: str) -> dict[str, str]:
    """Parse ECIA-like data from scanners that strip separators."""
    data = barcode

    for prefix in ("[)>06", ">[)>06"):
        if data.startswith(prefix):
            data = data[len(prefix) :]
            break

    output = {}

    sku = find_digikey_sku(data)
    if sku:
        output["supplier_part_number"] = sku

    if mpn := capture_between(data, "1P", DIGIKEY_FLAT_END_TOKENS):
        output["manufacturer_part_number"] = mpn

    quantity_end_tokens = [
        token for token in DIGIKEY_FLAT_END_TOKENS if token not in {"Q"}
    ]
    if quantity := capture_between(data, "Q", quantity_end_tokens):
        if re.fullmatch(r"\d+(?:\.\d+)?", quantity):
            output["quantity"] = quantity

    if supplier_order := capture_between(data, "1K", DIGIKEY_FLAT_END_TOKENS):
        output["supplier_order_number"] = supplier_order

    if date_code := capture_between(data, "9D", DIGIKEY_FLAT_END_TOKENS):
        output["date_code"] = date_code

    if lot_code := capture_between(data, "1T", DIGIKEY_FLAT_END_TOKENS):
        output["lot_code"] = lot_code

    return {key: value for key, value in output.items() if value}


def split_digikey_field(field_data: str) -> tuple[str | None, str]:
    """Split one DigiKey field into ECIA identifier and value."""
    for identifier in DIGIKEY_IDENTIFIERS:
        if field_data.startswith(identifier):
            return identifier, field_data[len(identifier) :]
    return None, field_data


def find_digikey_sku(data: str) -> str:
    """Find a DigiKey SKU in flat scanner output."""
    for identifier in ("30P", "P"):
        idx = data.find(identifier)
        if idx < 0:
            continue

        start = idx + len(identifier)
        suffix_match = re.search(r"[A-Z0-9][A-Z0-9._/-]*-ND", data[start:])
        if suffix_match and suffix_match.start() == 0:
            return suffix_match.group(0)

        value = capture_between(data, identifier, ["1P", "30P", "K", "1K", "10K", "9D", "1T", "4L", "Q"])
        if value:
            return value

    return ""


def capture_between(data: str, start_token: str, end_tokens: list[str]) -> str:
    """Capture flat text between one token and the nearest following token."""
    start = data.find(start_token)
    if start < 0:
        return ""

    value_start = start + len(start_token)
    value_end = len(data)

    for token in end_tokens:
        index = data.find(token, value_start)
        if index >= 0 and index < value_end:
            value_end = index

    return data[value_start:value_end].strip(DIGIKEY_SEPARATOR).strip()


def parse_decimal(value: str | None) -> Decimal | None:
    """Parse decimal quantities."""
    if value in (None, ""):
        return None

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
