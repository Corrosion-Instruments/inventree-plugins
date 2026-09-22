"""Guarded JLCPCB catalogue import for DipTrace BOM component rows.

The preview is read-only. Applying re-fetches the JLCPCB pages and rechecks
InvenTree identities so a stale preview cannot silently link the wrong part.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


CODE_RE = re.compile(r"C[1-9][0-9]*\Z", re.IGNORECASE)
MAX_ROWS = 500


class CatalogueError(ValueError):
    """A catalogue row cannot be imported safely."""


@dataclass(frozen=True)
class JlcPart:
    code: str
    manufacturer: str
    mpn: str
    description: str
    package: str
    url: str
    manufacturer_description: str = ""
    manufacturer_website: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_identifier(value: str) -> str:
    """Ignore only casing and incidental whitespace, not suffixes or punctuation."""
    return " ".join(str(value or "").split()).casefold()


def parse_jlc_page(html: str, code: str) -> JlcPart:
    """Extract the labelled product facts, failing closed if the page changes."""
    if not CODE_RE.fullmatch(code):
        raise CatalogueError(f"Invalid JLCPCB part number: {code!r}")
    soup = BeautifulSoup(html, "html.parser")
    fields: dict[str, str] = {}
    for label in soup.find_all("dt"):
        name = " ".join(label.get_text(" ", strip=True).split()).casefold()
        if name not in {"manufacturer", "mfr.part #", "jlcpcb part #", "package", "description"}:
            continue
        value = label.find_next_sibling("dd")
        if value is not None and name not in fields:
            fields[name] = " ".join(value.get_text(" ", strip=True).split())

    actual_code = fields.get("jlcpcb part #", "").upper()
    if actual_code != code.upper():
        raise CatalogueError(f"JLCPCB page identity does not match {code}")
    missing = [name for name in ("manufacturer", "mfr.part #", "description", "package") if not fields.get(name)]
    if missing:
        raise CatalogueError(f"JLCPCB page for {code} is missing: {', '.join(missing)}")
    return JlcPart(
        code=actual_code,
        manufacturer=fields["manufacturer"],
        mpn=fields["mfr.part #"],
        description=fields["description"],
        package=fields["package"],
        url=f"https://jlcpcb.com/partdetail/{actual_code}",
    )


class JlcPageClient:
    """Read only public JLCPCB product pages; never accept arbitrary URLs."""

    def __init__(self, session=None, timeout: int = 20):
        self.session = session or requests.Session()
        self.timeout = timeout

    def fetch(self, code: str) -> JlcPart:
        code = str(code or "").upper()
        if not CODE_RE.fullmatch(code):
            raise CatalogueError(f"Invalid JLCPCB part number: {code!r}")
        url = f"https://jlcpcb.com/partdetail/{code}"
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CatalogueError(f"Could not load JLCPCB page for {code}: {type(exc).__name__}") from exc
        if urlparse(response.url).hostname != "jlcpcb.com":
            raise CatalogueError(f"JLCPCB redirected {code} to another site")
        if len(response.content) > 3_000_000:
            raise CatalogueError(f"JLCPCB page for {code} is unexpectedly large")
        return parse_jlc_page(response.text, code)


def compare_row(row: dict, product: JlcPart) -> str | None:
    """Return a blocking mismatch message, or None for an exact MPN match."""
    sheet_mpn = str(row.get("footprint") or "").strip()
    if not sheet_mpn:
        return "The spreadsheet Footprint / manufacturer part number is blank"
    if normalize_identifier(sheet_mpn) != normalize_identifier(product.mpn):
        return f"Spreadsheet Footprint {sheet_mpn!r} differs from JLCPCB MFR Part # {product.mpn!r}"
    return None


class CatalogueImportService:
    """Plan and apply idempotent InvenTree catalogue records."""

    def __init__(self, client=None, api_client=None, supplier_id=None):
        self.client = client or JlcPageClient()
        self.api_client = api_client
        self.supplier_id = supplier_id

    def _supplier(self):
        from company.models import Company

        if self.supplier_id:
            supplier = Company.objects.filter(pk=self.supplier_id).first()
            if supplier is None or not supplier.is_supplier:
                raise CatalogueError("Configured JLC supplier is missing or not marked as a supplier")
            return supplier
        matches = list(Company.objects.filter(name__iexact="JLCPCB")[:2])
        if len(matches) > 1:
            raise CatalogueError("Multiple companies are named JLCPCB")
        if matches:
            if not matches[0].is_supplier:
                raise CatalogueError("JLCPCB company exists but is not marked as a supplier")
            return matches[0]
        aliases = list(Company.objects.filter(is_supplier=True, name__icontains="JLC"))
        jlcpcb_aliases = [company for company in aliases if _is_jlcpcb_name(company.name)]
        if len(jlcpcb_aliases) == 1:
            return jlcpcb_aliases[0]
        if aliases:
            raise CatalogueError("A JLC-named supplier already exists; select it in the plugin's JLC / LCSC Supplier setting")
        return None

    def _api_metadata(self, codes: list[str]) -> dict[str, dict]:
        """Optional company metadata; a missing/failed API never fabricates it."""
        if not self.api_client:
            return {}
        try:
            return self.api_client.component_details(codes)
        except Exception:
            return {}

    def preview(self, rows: list[dict]) -> dict:
        from part.models import PartCategory

        if not rows or len(rows) > MAX_ROWS:
            raise CatalogueError(f"Upload between 1 and {MAX_ROWS} component rows")
        duplicate_codes = _conflicting_codes(rows)
        api_metadata = self._api_metadata(sorted({str(row.get("jlcpcb_part") or "").upper() for row in rows if CODE_RE.fullmatch(str(row.get("jlcpcb_part") or "").upper())}))
        fetched: dict[str, JlcPart | CatalogueError] = {}
        results = []
        for row in rows:
            code = str(row.get("jlcpcb_part") or "").upper()
            result = {"row": row, "status": "blocked", "reason": "", "product": None, "part": None, "needs_category": False}
            if code in duplicate_codes:
                result["reason"] = "The same JLCPCB code has different manufacturer numbers in this file"
            elif not CODE_RE.fullmatch(code):
                result["reason"] = "A valid JLCPCB C-code is required"
            else:
                if code not in fetched:
                    try:
                        fetched[code] = self.client.fetch(code)
                    except CatalogueError as exc:
                        fetched[code] = exc
                product = fetched[code]
                if isinstance(product, CatalogueError):
                    result["reason"] = str(product)
                else:
                    metadata = _company_metadata(api_metadata.get(code, {}), product.manufacturer)
                    product = replace(product, **metadata)
                    result["product"] = product.as_dict()
                    mismatch = compare_row(row, product)
                    if mismatch:
                        result["reason"] = mismatch
                    else:
                        result.update(self._plan_product(product))
            results.append(result)

        categories = [
            {"pk": category.pk, "name": category.name, "path": category.pathstring, "structural": category.structural}
            for category in PartCategory.objects.all().order_by("name")
        ]
        return {
            "rows": results,
            "categories": categories,
            "summary": {
                "total": len(results),
                "ready": sum(item["status"] == "ready" for item in results),
                "complete": sum(item["status"] == "complete" for item in results),
                "blocked": sum(item["status"] == "blocked" for item in results),
            },
        }

    def _plan_product(self, product: JlcPart) -> dict:
        from company.models import Company, ManufacturerPart, SupplierPart
        from django.db.models import Q
        from part.models import Part
        from common.models import Parameter, ParameterTemplate
        from django.contrib.contenttypes.models import ContentType

        manufacturers = list(Company.objects.filter(name__iexact=product.manufacturer)[:2])
        if len(manufacturers) > 1:
            return _blocked("Duplicate manufacturer company names")
        manufacturer = manufacturers[0] if manufacturers else None
        try:
            supplier = self._supplier()
        except CatalogueError as exc:
            return _blocked(str(exc))
        if manufacturer is not None and not manufacturer.is_manufacturer:
            return _blocked("Existing manufacturer company is not marked as a manufacturer")

        maker_parts = list(ManufacturerPart.objects.filter(manufacturer=manufacturer, MPN__iexact=product.mpn)[:2]) if manufacturer else []
        supplier_parts = list(SupplierPart.objects.filter(supplier=supplier, SKU__iexact=product.code)[:2]) if supplier else []
        if len(maker_parts) > 1 or len(supplier_parts) > 1:
            return _blocked("Duplicate manufacturer MPN or JLCPCB supplier SKU")
        maker_part = maker_parts[0] if maker_parts else None
        supplier_part = supplier_parts[0] if supplier_parts else None
        if maker_part and supplier_part and maker_part.part_id != supplier_part.part_id:
            return _blocked("Manufacturer and supplier records point to different InvenTree Parts")
        if supplier_part and supplier_part.manufacturer_part_id not in (None, getattr(maker_part, "pk", None)):
            return _blocked("JLCPCB supplier record points to a different Manufacturer Part")
        part = maker_part.part if maker_part else supplier_part.part if supplier_part else None
        if part is None:
            candidates = list(Part.objects.filter(Q(IPN__iexact=product.mpn) | Q(name__iexact=product.mpn)).distinct()[:2])
            if len(candidates) > 1:
                return _blocked("Multiple existing InvenTree Parts match this manufacturer number")
            part = candidates[0] if candidates else None
        if part and ManufacturerPart.objects.filter(part=part).exclude(
            manufacturer=manufacturer, MPN__iexact=product.mpn
        ).exists():
            return _blocked("Existing Part has a different Manufacturer Part; resolve its identity manually")

        template = ParameterTemplate.objects.filter(name__iexact="Package").first()
        content_type = ContentType.objects.get_for_model(Part)
        if template and template.model_type_id not in (None, content_type.pk):
            return _blocked("Existing Package parameter template belongs to another model")
        if template and (template.checkbox or template.units or template.get_choices()):
            return _blocked("Existing Package parameter template is not a free-text package field")
        has_package = False
        if part and template:
            values = list(Parameter.objects.filter(model_type=content_type, model_id=part.pk, template=template)[:2])
            if len(values) > 1 or (values and normalize_identifier(values[0].data) != normalize_identifier(product.package)):
                return _blocked("Existing Part has a different Package parameter")
            has_package = bool(values)
        if part and part.locked and (not maker_part or not supplier_part or not has_package or not part.description):
            return _blocked("Existing Part is locked and would need catalogue changes")

        company_metadata_missing = bool(manufacturer and (
            (product.manufacturer_description and not manufacturer.description)
            or (product.manufacturer_website and not manufacturer.website)
        ))
        complete = bool(part and manufacturer and supplier and maker_part and supplier_part and has_package
                        and supplier_part.manufacturer_part_id == maker_part.pk
                        and part.description and not company_metadata_missing)
        return {
            "status": "complete" if complete else "ready",
            "reason": "All catalogue records already exist" if complete else "Ready to create missing catalogue records",
            "part": {"pk": part.pk, "name": part.name} if part else None,
            "needs_category": part is None,
        }

    def apply(self, rows: list[dict], category_ids: dict, user) -> dict:
        """Re-fetch source facts and re-plan inside one transaction before writing."""
        from common.models import Parameter, ParameterTemplate
        from company.models import Company, ManufacturerPart, SupplierPart
        from django.contrib.contenttypes.models import ContentType
        from django.core.exceptions import ValidationError
        from django.db import transaction
        from part.models import Part, PartCategory

        if not getattr(user, "is_superuser", False):
            raise CatalogueError("A superuser is required to apply catalogue imports")
        # Network lookups finish before opening the database transaction.
        # Source identities are fetched again here, independent of the signed preview.
        preview = self.preview(rows)
        with transaction.atomic():
            content_type = ContentType.objects.get_for_model(Part)
            output = {"created_parts": 0, "created_categories": 0, "created_manufacturers": 0, "created_supplier": 0,
                      "created_mpn": 0, "created_sku": 0, "created_package": 0,
                      "existing": 0, "skipped": []}
            for item in preview["rows"]:
                row = item["row"]
                code = str(row.get("jlcpcb_part") or "").upper()
                if item["status"] == "blocked":
                    output["skipped"].append({"code": code, "reason": item["reason"]})
                    continue
                product = JlcPart(**item["product"])
                fresh = self._plan_product(product)
                if fresh["status"] == "blocked":
                    output["skipped"].append({"code": code, "reason": fresh["reason"]})
                    continue
                if fresh["status"] == "complete":
                    output["existing"] += 1
                    continue
                category = None
                if fresh["needs_category"]:
                    choice = category_ids.get(str(row["row"])) or {}
                    if not isinstance(choice, dict):
                        choice = {"existing_id": choice}
                    existing_id = choice.get("existing_id")
                    new_name = " ".join(str(choice.get("new_name") or "").split())
                    parent_id = choice.get("parent_id")
                    if existing_id and new_name:
                        output["skipped"].append({"code": code, "reason": "Choose an existing category or enter a new name, not both"})
                        continue
                    if existing_id:
                        category = PartCategory.objects.filter(pk=existing_id, structural=False).first()
                    elif new_name:
                        if len(new_name) > 100:
                            output["skipped"].append({"code": code, "reason": "New category name exceeds 100 characters"})
                            continue
                        parent = PartCategory.objects.filter(pk=parent_id).first() if parent_id else None
                        if parent_id and parent is None:
                            output["skipped"].append({"code": code, "reason": "Selected parent category no longer exists"})
                            continue
                        peers = PartCategory.objects.filter(parent=parent, name__iexact=new_name)
                        if peers.count() > 1 or (peers.exists() and peers.first().structural):
                            output["skipped"].append({"code": code, "reason": "A conflicting category already exists under this parent"})
                            continue
                        category = peers.first()
                        if category is None:
                            category = PartCategory(name=new_name, parent=parent, structural=False)
                            try:
                                category.full_clean()
                            except ValidationError:
                                output["skipped"].append({"code": code, "reason": "New category name or parent is invalid"})
                                continue
                            category.save()
                            output["created_categories"] += 1
                    if category is None:
                        output["skipped"].append({"code": code, "reason": "Choose or create a non-structural category for the new Part"})
                        continue

                manufacturer = Company.objects.filter(name__iexact=product.manufacturer).first()
                if manufacturer is None:
                    manufacturer = Company.objects.create(name=product.manufacturer, description=product.manufacturer_description[:500],
                                                          website=product.manufacturer_website[:2000], is_manufacturer=True, active=True)
                    output["created_manufacturers"] += 1
                else:
                    changed = []
                    if product.manufacturer_description and not manufacturer.description:
                        manufacturer.description = product.manufacturer_description[:500]
                        changed.append("description")
                    if product.manufacturer_website and not manufacturer.website:
                        manufacturer.website = product.manufacturer_website[:2000]
                        changed.append("website")
                    if changed:
                        manufacturer.save(update_fields=changed)
                supplier = self._supplier()
                if supplier is None:
                    supplier = Company.objects.create(name="JLCPCB", is_supplier=True, active=True)
                    output["created_supplier"] += 1

                maker_part = ManufacturerPart.objects.filter(manufacturer=manufacturer, MPN__iexact=product.mpn).first()
                supplier_part = SupplierPart.objects.filter(supplier=supplier, SKU__iexact=product.code).first()
                part = maker_part.part if maker_part else supplier_part.part if supplier_part else None
                if part is None and fresh["part"]:
                    part = Part.objects.get(pk=fresh["part"]["pk"])
                if part is None:
                    part = Part.objects.create(name=product.mpn[:100], description=product.description[:250], category=category,
                                               component=True, purchaseable=True, active=True, creation_user=user)
                    output["created_parts"] += 1
                elif not part.description and not part.locked:
                    part.description = product.description[:250]
                    part.save(update_fields=["description"])

                if maker_part is None:
                    maker_part = ManufacturerPart.objects.create(part=part, manufacturer=manufacturer, MPN=product.mpn,
                                                                 description=product.description[:250], link=product.url)
                    output["created_mpn"] += 1
                if supplier_part is None:
                    SupplierPart.objects.create(part=part, supplier=supplier, SKU=product.code,
                                                manufacturer_part=maker_part, description=product.description[:250], link=product.url)
                    output["created_sku"] += 1
                elif supplier_part.manufacturer_part_id is None:
                    supplier_part.manufacturer_part = maker_part
                    supplier_part.save(update_fields=["manufacturer_part"])

                template = ParameterTemplate.objects.filter(name__iexact="Package").first()
                if template is None:
                    template = ParameterTemplate.objects.create(name="Package", model_type=content_type, enabled=True)
                if not Parameter.objects.filter(model_type=content_type, model_id=part.pk, template=template).exists():
                    parameter = Parameter(content_object=part, template=template, data=product.package)
                    parameter.full_clean()
                    parameter.save()
                    output["created_package"] += 1
            return output


def _blocked(reason: str) -> dict:
    return {"status": "blocked", "reason": reason, "part": None, "needs_category": False}


def _is_jlcpcb_name(name: str) -> bool:
    """Recognize only spelling variants of JLCPCB, not another JLC company."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").casefold()) == "jlcpcb"


def _conflicting_codes(rows: list[dict]) -> set[str]:
    seen: dict[str, str] = {}
    conflicts: set[str] = set()
    for row in rows:
        code = str(row.get("jlcpcb_part") or "").upper()
        mpn = normalize_identifier(row.get("footprint"))
        if code in seen and seen[code] != mpn:
            conflicts.add(code)
        seen.setdefault(code, mpn)
    return conflicts


def _company_metadata(record: dict, manufacturer_name: str) -> dict[str, str]:
    """Use company-specific API fields only, never component descriptions."""
    if not isinstance(record, dict):
        return {"manufacturer_description": "", "manufacturer_website": ""}
    api_name = str(record.get("componentBrandEn") or record.get("manufacturerName") or "")
    if api_name and normalize_identifier(api_name) != normalize_identifier(manufacturer_name):
        return {"manufacturer_description": "", "manufacturer_website": ""}
    description = str(record.get("manufacturerDescription") or record.get("brandDescription") or "").strip()
    website = str(record.get("manufacturerWebsite") or record.get("brandWebsite") or "").strip()
    parsed = urlparse(website)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        website = ""
    return {"manufacturer_description": description, "manufacturer_website": website}
