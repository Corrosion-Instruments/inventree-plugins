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
    source: str = "page"

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
    """Describe a sheet / JLC MPN difference, or return None for a match."""
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
        """Fetch component facts in one API request; page lookup remains a fallback."""
        if not self.api_client:
            return {}
        try:
            return self.api_client.component_details(codes)
        except Exception:
            return {}

    def _fetch_product(self, code: str, record: dict | None) -> JlcPart:
        """Prefer JLC API facts, using the page only for missing fields."""
        if not isinstance(record, dict) or not record:
            return self.client.fetch(code)
        api_code = str(record.get("componentCode") or record.get("component_code") or "").upper()
        if api_code != code:
            raise CatalogueError(f"JLCPCB API identity does not match {code}")
        manufacturer = str(record.get("componentBrandEn") or "").strip()
        mpn = str(record.get("componentModel") or "").strip()
        package = str(record.get("componentSpecification") or "").strip()
        description = str(record.get("description") or "").strip()
        if not all((manufacturer, mpn, package)):
            page = self.client.fetch(code)
            if mpn and normalize_identifier(mpn) != normalize_identifier(page.mpn):
                raise CatalogueError(f"JLCPCB API and page disagree on the MPN for {code}")
            if package and normalize_identifier(package) != normalize_identifier(page.package):
                raise CatalogueError(f"JLCPCB API and page disagree on the package for {code}")
            manufacturer = manufacturer or page.manufacturer
            mpn = mpn or page.mpn
            package = package or page.package
            description = description or page.description
            source = "api+page"
        else:
            source = "api"
        return JlcPart(code=code, manufacturer=manufacturer, mpn=mpn,
                       description=description, package=package,
                       url=f"https://jlcpcb.com/partdetail/{code}", source=source)

    def preview(self, rows: list[dict]) -> dict:
        from company.models import Company
        from part.models import PartCategory

        if not rows or len(rows) > MAX_ROWS:
            raise CatalogueError(f"Upload between 1 and {MAX_ROWS} component rows")
        duplicate_codes = _conflicting_codes(rows)
        api_metadata = self._api_metadata(sorted({str(row.get("jlcpcb_part") or "").upper() for row in rows if CODE_RE.fullmatch(str(row.get("jlcpcb_part") or "").upper())}))
        fetched: dict[str, JlcPart | CatalogueError] = {}
        results = []
        for row in rows:
            code = str(row.get("jlcpcb_part") or "").upper()
            result = {"row": row, "status": "blocked", "reason": "", "product": None, "part": None,
                      "needs_category": False, "needs_sheet_manufacturer": False,
                      "needs_jlc_manufacturer": False}
            if code in duplicate_codes:
                result["reason"] = "The same JLCPCB code has different manufacturer numbers in this file"
            elif not CODE_RE.fullmatch(code):
                result["reason"] = "A valid JLCPCB C-code is required"
            else:
                if code not in fetched:
                    try:
                        fetched[code] = self._fetch_product(code, api_metadata.get(code))
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
                    if not str(row.get("footprint") or "").strip():
                        result["reason"] = mismatch
                    else:
                        result.update(self._plan_product(product))
                        needs_sheet = bool(mismatch)
                        result["needs_sheet_manufacturer"] = needs_sheet
                        if needs_sheet and result["part"] and result["status"] == "complete":
                            # A previous import may already have attached the interchangeable
                            # sheet MPN to this same Part. Do not demand approval again.
                            from company.models import ManufacturerPart
                            sheet_parts = list(ManufacturerPart.objects.filter(
                                part_id=result["part"]["pk"], MPN__iexact=str(row["footprint"]).strip()
                            ).select_related("manufacturer")[:2])
                            if (len(sheet_parts) == 1 and sheet_parts[0].manufacturer.is_manufacturer
                                    and normalize_identifier(sheet_parts[0].manufacturer.name)
                                    != normalize_identifier(product.manufacturer)):
                                result["needs_sheet_manufacturer"] = False
                                result["reason"] = "Both manufacturer numbers already exist on this Part"
                                results.append(result)
                                continue
                        if needs_sheet and (result["status"] != "blocked" or result["reason"] == "Existing Part has a different Manufacturer Part; resolve its identity manually"):
                            if result["status"] == "blocked":
                                result["needs_category"] = True
                            result.update(status="review", reason=f"{mismatch}. Confirm interchangeability and select the spreadsheet MPN's manufacturer.")
                        if result["needs_jlc_manufacturer"] and result["status"] != "blocked":
                            result.update(status="review", reason=(
                                f"{result['reason']} Existing JLCPCB company {product.manufacturer!r} is not marked as a manufacturer; confirm changing its role."
                            ))
            results.append(result)

        categories = [
            {"pk": category.pk, "name": category.name, "path": category.pathstring,
             "parent_id": category.parent_id, "structural": category.structural}
            for category in PartCategory.objects.all().order_by("name")
        ]
        return {
            "rows": results,
            "categories": categories,
            "manufacturers": [
                {"pk": company.pk, "name": company.name, "is_manufacturer": company.is_manufacturer}
                for company in Company.objects.all().order_by("name")
            ],
            "summary": {
                "total": len(results),
                "ready": sum(item["status"] == "ready" for item in results),
                "review": sum(item["status"] == "review" for item in results),
                "complete": sum(item["status"] == "complete" for item in results),
                "blocked": sum(item["status"] == "blocked" for item in results),
            },
        }

    def _plan_product(self, product: JlcPart, sheet_mpn: str = "", sheet_manufacturer=None,
                      sheet_link: str = "") -> dict:
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
        needs_jlc_manufacturer = bool(manufacturer is not None and not manufacturer.is_manufacturer)

        maker_parts = list(ManufacturerPart.objects.filter(manufacturer=manufacturer, MPN__iexact=product.mpn)[:2]) if manufacturer else []
        sheet_parts = list(ManufacturerPart.objects.filter(manufacturer=sheet_manufacturer, MPN__iexact=sheet_mpn)[:2]) if getattr(sheet_manufacturer, "pk", None) else []
        supplier_parts = list(SupplierPart.objects.filter(supplier=supplier, SKU__iexact=product.code)[:2]) if supplier else []
        if len(maker_parts) > 1 or len(sheet_parts) > 1 or len(supplier_parts) > 1:
            return _blocked("Duplicate manufacturer MPN or JLCPCB supplier SKU")
        maker_part = maker_parts[0] if maker_parts else None
        sheet_part = sheet_parts[0] if sheet_parts else None
        supplier_part = supplier_parts[0] if supplier_parts else None
        if sheet_part and sheet_link and sheet_part.link and sheet_part.link != sheet_link:
            return _blocked("Existing spreadsheet Manufacturer Part has a different link; edit that record manually")
        linked_ids = {record.part_id for record in (maker_part, sheet_part, supplier_part) if record}
        if len(linked_ids) > 1:
            return _blocked("Manufacturer and supplier records point to different InvenTree Parts")
        if supplier_part and supplier_part.manufacturer_part_id not in (None, getattr(maker_part, "pk", None)):
            return _blocked("JLCPCB supplier record points to a different Manufacturer Part")
        part = maker_part.part if maker_part else supplier_part.part if supplier_part else sheet_part.part if sheet_part else None
        linked_part = part is not None
        if part is None:
            lookup_mpn = sheet_mpn or product.mpn
            candidates = list(Part.objects.filter(Q(IPN__iexact=lookup_mpn) | Q(name__iexact=lookup_mpn)).distinct()[:2])
            if len(candidates) > 1:
                return _blocked("Multiple existing InvenTree Parts match this manufacturer number")
            part = candidates[0] if candidates else None
        if part and not linked_part and ManufacturerPart.objects.filter(part=part).exclude(
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
        if part and part.locked and (not maker_part or not supplier_part or not has_package
                                    or (product.description and not part.description)
                                    or (sheet_manufacturer and not sheet_part) or (maker_part and not maker_part.link)
                                    or (sheet_link and sheet_part and not sheet_part.link)):
            return _blocked("Existing Part is locked and would need catalogue changes")

        company_metadata_missing = bool(manufacturer and (
            (product.manufacturer_description and not manufacturer.description)
            or (product.manufacturer_website and not manufacturer.website)
        ))
        complete = bool(part and manufacturer and manufacturer.is_manufacturer and supplier and maker_part and supplier_part and has_package
                        and (not sheet_manufacturer or sheet_part)
                        and maker_part.link
                        and (not sheet_link or (sheet_part and sheet_part.link == sheet_link))
                        and supplier_part.manufacturer_part_id == maker_part.pk
                        and (part.description or not product.description) and not company_metadata_missing)
        return {
            "status": "complete" if complete else "ready",
            "reason": "All catalogue records already exist" if complete else "Ready to create missing catalogue records",
            "part": {"pk": part.pk, "name": part.name} if part else None,
            "needs_category": part is None,
            "needs_jlc_manufacturer": needs_jlc_manufacturer,
        }

    def apply(self, rows: list[dict], category_ids: dict, manufacturer_choices: dict,
              sheet_links: dict,
              reviewed_mpns: dict, reviewed_manufacturers: dict, user, only_row=None) -> dict:
        """Re-fetch source facts and re-plan inside one transaction before writing."""
        from common.models import Parameter, ParameterTemplate
        from company.models import Company, ManufacturerPart, SupplierPart
        from django.contrib.contenttypes.models import ContentType
        from django.core.exceptions import ValidationError
        from django.db import transaction
        from part.models import Part, PartCategory

        if not getattr(user, "is_superuser", False):
            raise CatalogueError("A superuser is required to apply catalogue imports")
        rows = _select_apply_rows(rows, only_row)
        # Network lookups finish before opening the database transaction.
        # Source identities are fetched again here, independent of the signed preview.
        preview = self.preview(rows)
        with transaction.atomic():
            content_type = ContentType.objects.get_for_model(Part)
            output = {"created_parts": 0, "created_categories": 0, "created_manufacturers": 0,
                      "promoted_manufacturers": 0, "created_supplier": 0,
                      "created_mpn": 0, "created_sku": 0, "created_package": 0, "updated_links": 0,
                      "existing": 0, "saved_rows": [], "skipped": []}
            for item in preview["rows"]:
                row = item["row"]
                code = str(row.get("jlcpcb_part") or "").upper()
                row_key = str(row["row"])
                if item["status"] == "blocked":
                    output["skipped"].append({"code": code, "reason": item["reason"]})
                    continue
                product = JlcPart(**item["product"])
                if normalize_identifier(reviewed_mpns.get(row_key)) != normalize_identifier(product.mpn):
                    output["skipped"].append({"code": code, "reason": "JLCPCB MPN changed since preview; preview the file again"})
                    continue
                if normalize_identifier(reviewed_manufacturers.get(row_key)) != normalize_identifier(product.manufacturer):
                    output["skipped"].append({"code": code, "reason": "JLCPCB manufacturer changed since preview; preview the file again"})
                    continue
                choice = manufacturer_choices.get(row_key) or {}
                if not isinstance(choice, dict):
                    output["skipped"].append({"code": code, "reason": "Invalid manufacturer choice"})
                    continue
                if item["needs_jlc_manufacturer"] and choice.get("mark_jlc_manufacturer") is not True:
                    output["skipped"].append({"code": code, "reason": "Confirm marking the existing JLCPCB company as a manufacturer"})
                    continue
                sheet_mpn = ""
                sheet_manufacturer = None
                sheet_link = ""
                if item["needs_sheet_manufacturer"]:
                    try:
                        sheet_manufacturer = _resolve_sheet_manufacturer(choice, product.manufacturer)
                        sheet_link = validate_external_link(sheet_links.get(row_key))
                    except CatalogueError as exc:
                        output["skipped"].append({"code": code, "reason": str(exc)})
                        continue
                    sheet_mpn = str(row["footprint"]).strip()
                fresh = self._plan_product(product, sheet_mpn, sheet_manufacturer, sheet_link)
                if fresh["status"] == "blocked":
                    output["skipped"].append({"code": code, "reason": fresh["reason"]})
                    continue
                if fresh["status"] == "complete":
                    output["existing"] += 1
                    output["saved_rows"].append(row["row"])
                    continue
                category = None
                if fresh["needs_category"]:
                    choice = category_ids.get(row_key) or {}
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
                    if not manufacturer.is_manufacturer:
                        manufacturer.is_manufacturer = True
                        changed.append("is_manufacturer")
                        output["promoted_manufacturers"] += 1
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

                if sheet_manufacturer and sheet_manufacturer.pk is None:
                    sheet_manufacturer.save()
                    output["created_manufacturers"] += 1
                elif sheet_manufacturer and not sheet_manufacturer.is_manufacturer:
                    sheet_manufacturer.is_manufacturer = True
                    sheet_manufacturer.save(update_fields=["is_manufacturer"])
                    output["promoted_manufacturers"] += 1

                maker_part = ManufacturerPart.objects.filter(manufacturer=manufacturer, MPN__iexact=product.mpn).first()
                supplier_part = SupplierPart.objects.filter(supplier=supplier, SKU__iexact=product.code).first()
                sheet_part = ManufacturerPart.objects.filter(manufacturer=sheet_manufacturer, MPN__iexact=sheet_mpn).first() if sheet_manufacturer else None
                part = maker_part.part if maker_part else supplier_part.part if supplier_part else sheet_part.part if sheet_part else None
                if part is None and fresh["part"]:
                    part = Part.objects.get(pk=fresh["part"]["pk"])
                if part is None:
                    part = Part.objects.create(name=(sheet_mpn or product.mpn)[:100], description=product.description[:250], category=category,
                                               component=True, purchaseable=True, active=True, creation_user=user)
                    output["created_parts"] += 1
                elif not part.description and not part.locked:
                    part.description = product.description[:250]
                    part.save(update_fields=["description"])

                if maker_part is None:
                    maker_part = ManufacturerPart.objects.create(part=part, manufacturer=manufacturer, MPN=product.mpn,
                                                                 description=product.description[:250], link=product.url)
                    output["created_mpn"] += 1
                elif not maker_part.link:
                    maker_part.link = product.url
                    maker_part.save(update_fields=["link"])
                    output["updated_links"] += 1
                if sheet_manufacturer and sheet_part is None:
                    ManufacturerPart.objects.create(part=part, manufacturer=sheet_manufacturer, MPN=sheet_mpn,
                                                    description=product.description[:250], link=sheet_link)
                    output["created_mpn"] += 1
                elif sheet_part and sheet_link and not sheet_part.link:
                    sheet_part.link = sheet_link
                    sheet_part.save(update_fields=["link"])
                    output["updated_links"] += 1
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
                output["saved_rows"].append(row["row"])
            return output


def _blocked(reason: str) -> dict:
    return {"status": "blocked", "reason": reason, "part": None, "needs_category": False}


def validate_external_link(value: str | None) -> str:
    """Validate a saved part URL without fetching it."""
    link = str(value or "").strip()
    if not link:
        return ""
    parsed = urlparse(link)
    if (len(link) > 2000 or any(character.isspace() for character in link)
            or parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password):
        raise CatalogueError("Part link must be an http(s) URL without credentials or spaces")
    return link


def _resolve_sheet_manufacturer(choice: dict, jlc_name: str):
    """Validate explicit alternate-MPN approval without writing to the database."""
    from company.models import Company
    from django.core.exceptions import ValidationError

    existing_id, new_name = _validate_manufacturer_choice(choice)
    if existing_id:
        manufacturer = Company.objects.filter(pk=existing_id).first()
        if manufacturer is None:
            raise CatalogueError("Selected spreadsheet manufacturer no longer exists")
    else:
        matches = list(Company.objects.filter(name__iexact=new_name)[:2])
        if len(matches) > 1:
            raise CatalogueError("Multiple companies already have this manufacturer name")
        if matches:
            manufacturer = matches[0]
        else:
            manufacturer = Company(name=new_name, is_manufacturer=True, active=True)
            try:
                manufacturer.full_clean()
            except ValidationError as exc:
                raise CatalogueError("Spreadsheet manufacturer name is invalid") from exc
    if normalize_identifier(manufacturer.name) == normalize_identifier(jlc_name):
        raise CatalogueError("Choose the different manufacturer for the spreadsheet MPN")
    if not manufacturer.is_manufacturer and choice.get("mark_sheet_manufacturer") is not True:
        raise CatalogueError("Confirm marking the existing spreadsheet company as a manufacturer")
    return manufacturer


def _validate_manufacturer_choice(choice: dict) -> tuple[int | None, str]:
    """Require one manufacturer selection and a positive interchangeability decision."""
    if not isinstance(choice, dict) or choice.get("confirm") is not True:
        raise CatalogueError("Confirm that the spreadsheet and JLCPCB MPNs are interchangeable")
    raw_id = choice.get("existing_id")
    existing_id = None
    if raw_id:
        try:
            existing_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise CatalogueError("Selected spreadsheet manufacturer ID is invalid") from exc
        if existing_id <= 0:
            raise CatalogueError("Selected spreadsheet manufacturer ID is invalid")
    new_name = " ".join(str(choice.get("new_name") or "").split())
    if bool(existing_id) == bool(new_name):
        raise CatalogueError("Select one spreadsheet manufacturer or enter one new manufacturer name")
    if len(new_name) > 100:
        raise CatalogueError("Spreadsheet manufacturer name exceeds 100 characters")
    return existing_id, new_name


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


def _select_apply_rows(rows: list[dict], only_row) -> list[dict]:
    """Select one signed BOM line, retaining full-file C-code conflict checks."""
    if only_row is None:
        return rows
    try:
        selected = int(only_row)
    except (TypeError, ValueError) as exc:
        raise CatalogueError("Invalid selected BOM row") from exc
    matching = [row for row in rows if row.get("row") == selected]
    if len(matching) != 1:
        raise CatalogueError("Selected BOM row is missing or ambiguous")
    if str(matching[0].get("jlcpcb_part") or "").upper() in _conflicting_codes(rows):
        raise CatalogueError("The selected JLCPCB code has conflicting manufacturer numbers in this file")
    return matching


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
