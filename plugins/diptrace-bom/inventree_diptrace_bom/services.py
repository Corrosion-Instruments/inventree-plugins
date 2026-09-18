"""InvenTree-side matching, availability and BOM finalization services."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any

from .jlcpcb import (
    JlcApiError,
    JlcClient,
    JlcCredentials,
    availability_summary,
)


class BomImportError(RuntimeError):
    """User-facing BOM import error."""


class BomImportService:
    """Coordinate deterministic InvenTree matching and JLCPCB lookups."""

    def __init__(self, plugin):
        self.plugin = plugin

    def preview(self, rows: list[dict], assembly_id: int | None) -> dict[str, Any]:
        """Enrich normalized rows with local and external availability."""
        assembly = self.assembly(assembly_id, required=False)
        matches = [self.match_row(row) for row in rows]
        jlc, jlc_error = self.jlc_availability(
            [row.get("jlcpcb_part", "") for row in rows]
        )

        enriched = []
        total_quantity = Decimal("0")
        assembly_can_build = None
        for row, match in zip(rows, matches, strict=True):
            quantity = _positive_decimal(row.get("quantity"), "quantity")
            total_quantity += quantity
            part = match.get("part")
            available = Decimal(str(part.available_stock)) if part else Decimal("0")
            line_can_build = int((available / quantity).to_integral_value(rounding=ROUND_FLOOR))
            if part is not None:
                assembly_can_build = (
                    line_can_build
                    if assembly_can_build is None
                    else min(assembly_can_build, line_can_build)
                )
            else:
                assembly_can_build = 0

            enriched.append(
                {
                    **row,
                    "match": {
                        "status": match["status"],
                        "reason": match["reason"],
                        "part": part_summary(part) if part else None,
                        "candidates": [part_summary(candidate) for candidate in match["candidates"]],
                    },
                    "local": {
                        "available": _decimal_string(available),
                        "can_build": line_can_build,
                    },
                    "jlc": jlc.get(row.get("jlcpcb_part", "").upper(), empty_jlc()),
                }
            )

        return {
            "assembly": part_summary(assembly) if assembly else None,
            "rows": enriched,
            "summary": {
                "line_count": len(enriched),
                "component_quantity": _decimal_string(total_quantity),
                "matched": sum(1 for row in enriched if row["match"]["status"] == "matched"),
                "unresolved": sum(1 for row in enriched if row["match"]["status"] != "matched"),
                "can_build": assembly_can_build or 0,
            },
            "jlc_error": jlc_error,
            "settings": {
                "jlc_configured": self.jlc_credentials().configured,
                "allow_create_missing": bool(self.setting("ALLOW_CREATE_MISSING", False)),
                "default_category": self.setting("DEFAULT_COMPONENT_CATEGORY", None),
            },
        }

    def finalize(
        self,
        rows: list[dict],
        *,
        assembly_id: int,
        selections: dict,
        mode: str,
        validate: bool,
        create_missing: bool,
        user,
    ) -> dict[str, Any]:
        """Create or update the InvenTree BOM in a single transaction."""
        from django.db import transaction
        from part.models import BomItem, Part

        if mode not in {"merge", "replace"}:
            raise BomImportError("Import mode must be 'merge' or 'replace'")

        if create_missing and not bool(self.setting("ALLOW_CREATE_MISSING", False)):
            raise BomImportError("Creating missing parts is disabled in plugin settings")

        with transaction.atomic():
            assembly = Part.objects.select_for_update().filter(pk=assembly_id).first()
            if not assembly:
                raise BomImportError("The selected assembly part was not found")
            if not assembly.assembly:
                raise BomImportError("The selected part is not marked as an assembly")

            resolved: list[tuple[dict, Any]] = []
            created_parts = []
            for row in rows:
                key = str(row["row"])
                selected_id = selections.get(key) or selections.get(row["row"])
                part = Part.objects.filter(pk=selected_id).first() if selected_id else None
                if not part:
                    match = self.match_row(row)
                    part = match.get("part") if match["status"] == "matched" else None
                if not part and create_missing:
                    part = self.create_component(row, user=user)
                    created_parts.append(part)
                if not part:
                    code = row.get("jlcpcb_part") or row.get("comment") or f"row {key}"
                    raise BomImportError(f"Resolve {code} to an InvenTree part before finalizing")
                if part.pk == assembly.pk:
                    raise BomImportError("An assembly cannot contain itself")
                resolved.append((row, part))

            # Merge source rows that resolve to the same physical component.
            merged: dict[int, dict] = {}
            for row, part in resolved:
                entry = merged.setdefault(
                    part.pk,
                    {"part": part, "quantity": Decimal("0"), "references": [], "notes": []},
                )
                entry["quantity"] += _positive_decimal(row.get("quantity"), "quantity")
                entry["references"].extend(_split_references(row.get("designators", "")))
                note = _line_note(row)
                if note and note not in entry["notes"]:
                    entry["notes"].append(note)

            deleted = 0
            if mode == "replace":
                for item in list(BomItem.objects.filter(part=assembly)):
                    item.delete()
                    deleted += 1

            created = 0
            updated = 0
            imported_ids = []
            for entry in merged.values():
                existing = list(
                    BomItem.objects.filter(part=assembly, sub_part=entry["part"]).order_by("pk")[:2]
                )
                if len(existing) > 1:
                    raise BomImportError(
                        f"The existing BOM contains duplicate lines for {entry['part'].full_name}"
                    )
                item = existing[0] if existing else BomItem(part=assembly, sub_part=entry["part"])
                item.raw_amount = _decimal_string(entry["quantity"])
                item.quantity = entry["quantity"]
                item.reference = ", ".join(_deduplicate(entry["references"]))[:5000]
                item.note = " | ".join(entry["notes"])[:500]
                item.save()
                if validate:
                    item.validate_hash(True)
                imported_ids.append(item.pk)
                if existing:
                    updated += 1
                else:
                    created += 1

            assembly.refresh_from_db()
            return {
                "assembly": part_summary(assembly),
                "bom_url": f"/web/part/{assembly.pk}/bom/",
                "created": created,
                "updated": updated,
                "deleted": deleted,
                "created_parts": [part_summary(part) for part in created_parts],
                "bom_item_ids": imported_ids,
                "can_build": int(assembly.can_build),
            }

    def match_row(self, row: dict) -> dict:
        """Match only on stable exact identifiers; never silently fuzzy-match."""
        from company.models import ManufacturerPart, SupplierPart
        from part.models import Part

        code = str(row.get("jlcpcb_part") or "").strip()
        if code:
            supplier_parts = SupplierPart.objects.filter(SKU__iexact=code).select_related("part")
            supplier_id = self.setting("JLC_SUPPLIER", None)
            preferred = supplier_parts.filter(supplier_id=supplier_id) if supplier_id else supplier_parts.none()
            preferred_parts = _unique_parts(preferred)
            if len(preferred_parts) == 1:
                return _match(preferred_parts[0], "matched", f"JLC supplier SKU {code}")
            if len(preferred_parts) > 1:
                return _match(None, "ambiguous", f"Multiple JLC supplier parts use SKU {code}", preferred_parts)

            supplier_parts = _unique_parts(supplier_parts)
            if len(supplier_parts) == 1:
                return _match(supplier_parts[0], "matched", f"Supplier SKU {code}")
            if len(supplier_parts) > 1:
                return _match(None, "ambiguous", f"Multiple supplier parts use SKU {code}", supplier_parts)

            exact_parts = _unique_parts(
                Part.objects.filter(IPN__iexact=code) | Part.objects.filter(name__iexact=code)
            )
            if len(exact_parts) == 1:
                return _match(exact_parts[0], "matched", f"Exact part identifier {code}")
            if len(exact_parts) > 1:
                return _match(None, "ambiguous", f"Multiple parts use identifier {code}", exact_parts)

        comment = str(row.get("comment") or "").strip()
        if comment:
            manufacturer_parts = _unique_parts(
                ManufacturerPart.objects.filter(MPN__iexact=comment).select_related("part")
            )
            if len(manufacturer_parts) == 1:
                return _match(manufacturer_parts[0], "matched", f"Manufacturer MPN {comment}")
            if len(manufacturer_parts) > 1:
                return _match(None, "ambiguous", f"Multiple manufacturer parts use MPN {comment}", manufacturer_parts)

            exact_parts = _unique_parts(
                Part.objects.filter(IPN__iexact=comment) | Part.objects.filter(name__iexact=comment)
            )
            if len(exact_parts) == 1:
                return _match(exact_parts[0], "matched", f"Exact part name/IPN {comment}")

        return _match(None, "unmatched", "No exact InvenTree identifier match")

    def search_parts(self, query: str, limit: int = 30) -> list[dict]:
        """Search parts and linked supplier/manufacturer identifiers."""
        query = str(query or "").strip()
        if len(query) < 2:
            return []
        from company.models import ManufacturerPart, SupplierPart
        from django.db.models import Q
        from part.models import Part

        supplier_ids = SupplierPart.objects.filter(SKU__icontains=query).values_list("part_id", flat=True)
        manufacturer_ids = ManufacturerPart.objects.filter(MPN__icontains=query).values_list("part_id", flat=True)
        parts = (
            Part.objects.filter(
                Q(name__icontains=query)
                | Q(IPN__icontains=query)
                | Q(description__icontains=query)
                | Q(pk__in=supplier_ids)
                | Q(pk__in=manufacturer_ids)
            )
            .distinct()
            .order_by("name")[:limit]
        )
        return [part_summary(part) for part in parts]

    def search_assemblies(self, query: str, limit: int = 30) -> list[dict]:
        """Search assembly parts for the upload target."""
        from django.db.models import Q
        from part.models import Part

        query = str(query or "").strip()
        parts = Part.objects.filter(assembly=True)
        if query:
            criteria = Q(name__icontains=query) | Q(IPN__icontains=query) | Q(description__icontains=query)
            if query.isdigit():
                criteria |= Q(pk=int(query))
            parts = parts.filter(criteria)
        return [part_summary(part) for part in parts.order_by("name")[:limit]]

    def create_component(self, row: dict, *, user):
        """Create a minimal component and JLC supplier record when explicitly enabled."""
        from company.models import Company, SupplierPart
        from part.models import Part, PartCategory

        category_id = self.setting("DEFAULT_COMPONENT_CATEGORY", None)
        category = PartCategory.objects.filter(pk=category_id).first() if category_id else None
        if not category:
            raise BomImportError("Configure a default component category before creating missing parts")

        supplier_id = self.setting("JLC_SUPPLIER", None)
        supplier = Company.objects.filter(pk=supplier_id, is_supplier=True).first() if supplier_id else None
        if not supplier:
            raise BomImportError("Configure the JLC/LCSC supplier before creating missing parts")

        code = str(row.get("jlcpcb_part") or "").strip()
        name = (str(row.get("comment") or "").strip() or code or "Imported component")[:100]
        description = " | ".join(
            value
            for value in (
                str(row.get("footprint") or "").strip(),
                f"JLCPCB {code}" if code else "",
            )
            if value
        )[:250]
        part = Part(
            name=name,
            description=description,
            category=category,
            purchaseable=True,
            component=True,
            active=True,
            creation_user=user,
        )
        part.save()
        if code:
            SupplierPart.objects.create(
                part=part,
                supplier=supplier,
                SKU=code,
                description=description,
            )
        return part

    def jlc_availability(self, codes: list[str]) -> tuple[dict[str, dict], str]:
        """Return public and private JLC stock without changing InvenTree stock."""
        codes = list(dict.fromkeys(str(code or "").upper() for code in codes if code))
        if not codes:
            return {}, ""
        credentials = self.jlc_credentials()
        if not credentials.configured:
            return {}, "JLCPCB API credentials are not configured"

        from django.core.cache import cache

        client = JlcClient(
            credentials,
            host=str(self.setting("JLC_HOST", "https://open.jlcpcb.com")),
            timeout=int(self.setting("JLC_TIMEOUT", 30)),
        )
        try:
            public = {}
            missing = []
            for code in codes:
                cached = cache.get(f"diptrace-bom:jlc:public:{code}")
                if cached is None:
                    missing.append(code)
                else:
                    public[code] = cached
            if missing:
                fetched = client.component_details(missing)
                public.update(fetched)
                for code in missing:
                    cache.set(f"diptrace-bom:jlc:public:{code}", fetched.get(code, {}), 900)

            private = cache.get("diptrace-bom:jlc:private")
            if private is None:
                private = client.private_library()
                cache.set("diptrace-bom:jlc:private", private, 300)

            return {
                code: _json_safe(availability_summary(public.get(code), private.get(code)))
                for code in codes
            }, ""
        except JlcApiError as exc:
            return {}, str(exc)

    def jlc_credentials(self) -> JlcCredentials:
        return JlcCredentials(
            app_id=str(self.setting("JLC_APP_ID", "")),
            access_key=str(self.setting("JLC_ACCESS_KEY", "")),
            tokenization_key=str(self.setting("JLC_TOKENIZATION_KEY", "")),
        )

    def assembly(self, assembly_id, *, required: bool):
        from part.models import Part

        assembly = Part.objects.filter(pk=assembly_id, assembly=True).first() if assembly_id else None
        if required and not assembly:
            raise BomImportError("Select a valid assembly part")
        return assembly

    def setting(self, key: str, default=None):
        try:
            value = self.plugin.get_setting(key, cache=True)
        except Exception:
            return default
        return default if value in (None, "") else value


def part_summary(part) -> dict | None:
    if not part:
        return None
    return {
        "pk": part.pk,
        "name": part.name,
        "ipn": getattr(part, "IPN", "") or "",
        "description": getattr(part, "description", "") or "",
        "label": part.full_name,
        "category": getattr(getattr(part, "category", None), "pathstring", "") or "",
        "web_url": f"/web/part/{part.pk}/",
    }


def empty_jlc() -> dict:
    return {
        "public_stock": "0",
        "private_total": "0",
        "jlcpcb_parts": "0",
        "global_sourcing": "0",
        "consigned": "0",
        "idle_stock": "0",
        "model": "",
        "specification": "",
        "brand": "",
    }


def _match(part, status: str, reason: str, candidates=None) -> dict:
    return {"part": part, "status": status, "reason": reason, "candidates": candidates or []}


def _unique_parts(queryset) -> list:
    parts = []
    seen = set()
    for value in queryset[:25]:
        part = getattr(value, "part", value)
        if part and part.pk not in seen:
            seen.add(part.pk)
            parts.append(part)
    return parts


def _positive_decimal(value, field: str) -> Decimal:
    try:
        value = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise BomImportError(f"Invalid {field}") from None
    if value <= 0:
        raise BomImportError(f"{field.capitalize()} must be greater than zero")
    return value


def _decimal_string(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _split_references(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").replace(";", ",").split(",") if item.strip()]


def _deduplicate(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _line_note(row: dict) -> str:
    values = []
    if row.get("comment"):
        values.append(str(row["comment"]))
    if row.get("footprint"):
        values.append(f"Footprint: {row['footprint']}")
    if row.get("jlcpcb_part"):
        values.append(f"JLCPCB: {row['jlcpcb_part']}")
    return " | ".join(values)


def _json_safe(value):
    if isinstance(value, Decimal):
        return _decimal_string(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value
