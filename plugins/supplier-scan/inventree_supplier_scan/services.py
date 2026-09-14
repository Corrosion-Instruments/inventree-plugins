"""InvenTree database operations for supplier scans."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Any
from urllib.parse import urlparse

from .barcodes import BarcodeParseError, ParsedBarcode, parse_barcode
from .suppliers import SupplierPartSource, lean_source, lookup_source_part


class SupplierScanError(RuntimeError):
    """Raised for user-facing supplier scan errors."""


class DuplicateBarcodeError(SupplierScanError):
    """Raised when the same barcode was already received."""

    def __init__(self, stock_item):
        super().__init__("This supplier barcode has already been received")
        self.stock_item = stock_item


@dataclass
class CategorySuggestion:
    """Category suggestion for a supplier source."""

    pk: int | None
    path: str
    confidence: str

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return {"pk": self.pk, "path": self.path, "confidence": self.confidence}


class SupplierScanService:
    """Service object for previewing and receiving supplier scans."""

    def __init__(self, plugin):
        self.plugin = plugin

    def preview(self, barcode: str) -> dict[str, Any]:
        """Preview a supplier barcode scan."""
        try:
            parsed = parse_barcode(barcode)
        except BarcodeParseError as exc:
            raise SupplierScanError(str(exc)) from exc

        source = self.lookup_source(parsed)
        supplier_part = self.find_supplier_part(parsed)
        part = supplier_part.part if supplier_part else self.find_existing_part(source)
        stock_item = self.find_duplicate_stock_item(barcode)
        category = None

        if part:
            category = CategorySuggestion(
                pk=getattr(part.category, "pk", None),
                path=getattr(part.category, "pathstring", "") if part.category else "",
                confidence="existing_part",
            )
        else:
            category = self.suggest_category(source)

        auto_receive = bool(self.setting("AUTO_CREATE", False)) and not stock_item
        auto_receive = auto_receive and (
            supplier_part is not None
            or part is not None
            or (category is not None and category.pk is not None and category.confidence == "exact")
        )

        return {
            "parsed": parsed.as_dict(),
            "source": source.as_dict(),
            "supplier_part": object_summary(supplier_part),
            "part": object_summary(part),
            "part_candidates": [] if part else self.part_candidates(source),
            "duplicate_stock_item": object_summary(stock_item),
            "category_suggestion": category.as_dict() if category else None,
            "default_location": object_summary(self.default_location(create=False)),
            "auto_receive": auto_receive,
            "settings": {
                "auto_create": bool(self.setting("AUTO_CREATE", False)),
                "block_duplicates": bool(self.setting("BLOCK_DUPLICATE_BARCODES", True)),
            },
        }

    def receive(
        self,
        barcode: str,
        *,
        user,
        category_id: int | None = None,
        location_id: int | None = None,
        quantity: str | int | float | Decimal | None = None,
        allow_duplicate: bool = False,
        part_id: int | None = None,
        use_supplier_image: bool = True,
    ) -> dict[str, Any]:
        """Receive a barcode into stock."""
        from django.db import transaction

        try:
            parsed = parse_barcode(barcode)
        except BarcodeParseError as exc:
            raise SupplierScanError(str(exc)) from exc

        if (
            self.setting("BLOCK_DUPLICATE_BARCODES", True)
            and not allow_duplicate
            and (duplicate := self.find_duplicate_stock_item(barcode))
        ):
            raise DuplicateBarcodeError(duplicate)

        with transaction.atomic():
            source = self.lookup_source(parsed)
            supplier = self.supplier_company(parsed.supplier, create=True)
            supplier_part = self.find_supplier_part(parsed, supplier=supplier)
            created_part = False
            created_supplier_part = False

            if supplier_part:
                part = supplier_part.part
            else:
                part = self.part_by_id(part_id) if part_id else self.find_existing_part(source)
                if part_id and not part:
                    raise SupplierScanError("Selected part was not found")
                if not part:
                    category = self.resolve_category(category_id, source)
                    if not category:
                        raise SupplierScanError("Select a category before creating this part")
                    part = self.create_part(source, category, user)
                    created_part = True

                manufacturer_part = self.ensure_manufacturer_part(source, part)
                supplier_part = self.ensure_supplier_part(
                    source, part, supplier, manufacturer_part
                )
                created_supplier_part = True

            self.sync_price_breaks(supplier_part, source)

            stock_item = self.create_stock_item(
                barcode,
                parsed,
                source,
                supplier_part,
                location_id=location_id,
                quantity=quantity,
                user=user,
                allow_duplicate=allow_duplicate,
            )

        metadata_sync = self.sync_part_metadata(
            supplier_part.part,
            source,
            user=user,
            use_supplier_image=use_supplier_image,
        )

        return {
            "success": True,
            "created_part": created_part,
            "created_supplier_part": created_supplier_part,
            "parsed": parsed.as_dict(),
            "source": source.as_dict(),
            "part": object_summary(supplier_part.part),
            "supplier_part": object_summary(supplier_part),
            "stock_item": object_summary(stock_item),
            "quantity": str(stock_item.quantity),
            "metadata_sync": metadata_sync,
        }

    def lookup_source(self, parsed: ParsedBarcode) -> SupplierPartSource:
        """Look up rich supplier data where possible."""
        if parsed.supplier == "digikey" and not (
            self.setting("DIGIKEY_CLIENT_ID", "") and self.setting("DIGIKEY_CLIENT_SECRET", "")
        ):
            return lean_source(parsed)

        return lookup_source_part(
            parsed,
            digikey_client_id=self.setting("DIGIKEY_CLIENT_ID", ""),
            digikey_client_secret=self.setting("DIGIKEY_CLIENT_SECRET", ""),
            digikey_currency=self.setting("DIGIKEY_CURRENCY", "AUD"),
            digikey_language=self.setting("DIGIKEY_LANGUAGE", "en"),
            digikey_location=self.setting("DIGIKEY_LOCATION", "AU"),
        )

    def setting(self, key: str, default=None):
        """Read a plugin setting."""
        try:
            value = self.plugin.get_setting(key, cache=True)
        except Exception:
            return default
        return default if value in (None, "") else value

    def find_supplier_part(self, parsed: ParsedBarcode, supplier=None):
        """Find an existing supplier part for the parsed barcode."""
        from company.models import SupplierPart

        query = SupplierPart.objects.filter(SKU__iexact=parsed.sku)
        if supplier:
            query = query.filter(supplier=supplier)
        else:
            configured_supplier = self.supplier_company(parsed.supplier, create=False)
            if configured_supplier:
                query = query.filter(supplier=configured_supplier)

        if query.count() == 1:
            return query.first()

        # Fallback: if no supplier is configured and exactly one SKU exists anywhere.
        fallback = SupplierPart.objects.filter(SKU__iexact=parsed.sku)
        if fallback.count() == 1:
            return fallback.first()

        return None

    def find_existing_part(self, source: SupplierPartSource):
        """Find an existing InvenTree part by manufacturer part number."""
        from company.models import ManufacturerPart
        from part.models import Part

        if source.mpn:
            matches = ManufacturerPart.objects.filter(MPN__iexact=source.mpn)
            if matches.count() == 1:
                return matches.first().part

            for field in ("name", "IPN"):
                part = unique_part_match(Part, field, source.mpn)
                if part:
                    return part

        if source.name and source.name != source.mpn:
            return unique_part_match(Part, "name", source.name)

        return None

    def part_by_id(self, part_id):
        """Return a part by primary key."""
        if not part_id:
            return None

        from part.models import Part

        return Part.objects.filter(pk=part_id).first()

    def part_candidates(self, source: SupplierPartSource, limit: int = 10) -> list[dict[str, Any]]:
        """Return possible existing part matches for user confirmation."""
        from django.db.models import Q

        from company.models import ManufacturerPart, SupplierPart
        from part.models import Part

        candidates: dict[int, dict[str, Any]] = {}

        def add(part, reason: str, score: int):
            if not part:
                return
            current = candidates.get(part.pk)
            if current and current.get("score", 0) >= score:
                return
            row = part_summary(part, reason=reason)
            row["score"] = score
            candidates[part.pk] = row

        if source.sku:
            for supplier_part in SupplierPart.objects.filter(SKU__iexact=source.sku)[:limit]:
                add(supplier_part.part, f"Supplier SKU {source.sku}", 100)

        if source.mpn:
            for manufacturer_part in ManufacturerPart.objects.filter(MPN__iexact=source.mpn)[:limit]:
                add(manufacturer_part.part, f"MPN {source.mpn}", 90)

        if source.name:
            for part in Part.objects.filter(name__iexact=source.name)[:limit]:
                add(part, f"Name {source.name}", 80)

        text_terms = [
            source.name,
            f"{source.manufacturer} {source.mpn}".strip(),
            source.manufacturer,
        ]

        for term in [term for term in text_terms if len(term or "") >= 4]:
            query = Q(name__icontains=term) | Q(IPN__icontains=term) | Q(
                description__icontains=term
            )
            for part in Part.objects.filter(query).order_by("name")[:limit]:
                add(part, f"Contains {term}", 40)

        return sorted(candidates.values(), key=lambda row: (-row["score"], row["label"]))[
            :limit
        ]

    def search_parts(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search existing parts for manual selection."""
        query = str(query or "").strip()
        if len(query) < 2:
            return []

        from django.db.models import Q

        from company.models import ManufacturerPart, SupplierPart
        from part.models import Part

        part_ids = set(
            ManufacturerPart.objects.filter(MPN__icontains=query).values_list(
                "part_id", flat=True
            )[:limit]
        )
        part_ids.update(
            SupplierPart.objects.filter(SKU__icontains=query).values_list(
                "part_id", flat=True
            )[:limit]
        )

        part_query = (
            Q(name__icontains=query)
            | Q(IPN__icontains=query)
            | Q(description__icontains=query)
            | Q(pk__in=part_ids)
        )

        return [
            part_summary(part, reason="Search result")
            for part in Part.objects.filter(part_query).distinct().order_by("name")[:limit]
        ]

    def supplier_company(self, supplier: str, *, create: bool):
        """Return or create the InvenTree supplier company."""
        from company.models import Company

        setting_key = "LCSC_SUPPLIER" if supplier == "lcsc" else "DIGIKEY_SUPPLIER"
        configured = self.setting(setting_key, None)
        if configured:
            company = Company.objects.filter(pk=configured).first()
            if company:
                return company

        name = "LCSC" if supplier == "lcsc" else "DigiKey"
        company = Company.objects.filter(name__iexact=name).first()
        if company and not company.is_supplier and create:
            company.is_supplier = True
            company.save()

        if company or not create:
            return company

        company = Company(name=name, is_supplier=True, is_manufacturer=False)
        company.save()
        return company

    def find_duplicate_stock_item(self, barcode: str):
        """Return an existing stock item for the exact supplier barcode."""
        from InvenTree.helpers import hash_barcode
        from stock.models import StockItem

        return StockItem.lookup_barcode(hash_barcode(barcode))

    def suggest_category(self, source: SupplierPartSource) -> CategorySuggestion | None:
        """Suggest a category from supplier metadata."""
        from part.models import PartCategory

        categories = list(
            PartCategory.objects.filter(structural=False).order_by("tree_id", "lft")
        )
        if not categories:
            categories = list(PartCategory.objects.all().order_by("tree_id", "lft"))

        indexed = category_index(categories)
        for name in reversed(source.category_path):
            for key, confidence in category_lookup_keys(name):
                category = unique_category_for_key(indexed, key)
                if category:
                    return CategorySuggestion(category.pk, category.pathstring, confidence)

        match = best_source_category_match(source, categories)
        if match:
            category, confidence = match
            return CategorySuggestion(category.pk, category.pathstring, confidence)

        return None

    def resolve_category(self, category_id: int | None, source: SupplierPartSource):
        """Resolve an explicit or confident category."""
        from part.models import PartCategory

        if category_id:
            return PartCategory.objects.filter(pk=category_id).first()

        if bool(self.setting("AUTO_CREATE", False)):
            suggestion = self.suggest_category(source)
            if suggestion and suggestion.pk and suggestion.confidence == "exact":
                return PartCategory.objects.filter(pk=suggestion.pk).first()

        return None

    def create_part(self, source: SupplierPartSource, category, user):
        """Create an InvenTree part."""
        from part.models import Part

        name = (source.mpn or source.name or source.sku)[:100]
        description = (source.description or source.name or source.sku)[:250]
        part = Part(
            name=name,
            description=description,
            category=category,
            link=source.supplier_link[:2000],
            purchaseable=True,
            component=True,
            creation_user=user,
            notes=source_notes(source),
        )
        part.save()
        return part

    def sync_part_metadata(
        self,
        part,
        source: SupplierPartSource,
        *,
        user,
        use_supplier_image: bool = True,
    ) -> dict[str, Any]:
        """Attach rich supplier metadata to a part where possible."""
        result = {
            "image_requested": bool(use_supplier_image),
            "image_attached": False,
            "parameters_synced": 0,
            "parameters_skipped": 0,
            "errors": [],
        }

        if use_supplier_image:
            try:
                result["image_attached"] = self.attach_part_image(part, source)
            except Exception as exc:
                result["errors"].append(f"Image import failed: {exc}")

        try:
            synced, skipped = self.sync_part_parameters(part, source, user=user)
            result["parameters_synced"] = synced
            result["parameters_skipped"] = skipped
        except Exception as exc:
            result["errors"].append(f"Parameter import failed: {exc}")

        return result

    def attach_part_image(self, part, source: SupplierPartSource) -> bool:
        """Download and attach the supplier image to the part if it has no image."""
        if not part or not source.image_url or getattr(part, "image", None):
            return False

        from django.core.files.base import ContentFile
        import requests

        image_url = normalize_supplier_image_url(source.image_url, source.supplier)
        response = requests.get(
            image_url,
            headers={"User-Agent": "Mozilla/5.0 InvenTreeSupplierScan/0.1"},
            timeout=15,
        )
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
        if content_type and not content_type.startswith("image/"):
            raise SupplierScanError(f"Supplier image returned {content_type}")

        content = response.content
        if not content:
            return False
        if len(content) > 5 * 1024 * 1024:
            raise SupplierScanError("Supplier image is larger than 5 MB")

        extension = image_extension(image_url, content_type)
        filename = safe_filename(f"{source.supplier}_{source.sku}.{extension}")
        part.image.save(filename, ContentFile(content), save=True)
        return True

    def sync_part_parameters(self, part, source: SupplierPartSource, *, user) -> tuple[int, int]:
        """Sync supplier parameters to InvenTree Parameter records."""
        if not part or not source.parameters:
            return 0, 0

        from django.contrib.contenttypes.models import ContentType
        from common.models import Parameter, ParameterTemplate
        from part.models import Part, PartCategoryParameterTemplate

        content_type = ContentType.objects.get_for_model(Part)
        category_templates = []
        if part.category:
            categories = part.category.get_ancestors(include_self=True)
            category_templates = list(
                PartCategoryParameterTemplate.objects.filter(
                    category__in=categories
                ).select_related("template")
            )

        existing_templates = list(ParameterTemplate.objects.all())
        synced = 0
        skipped = 0

        for raw_name, raw_value in source.parameters.items():
            name = clean_parameter_name(raw_name)
            value = clean_parameter_value(raw_value)
            if not name or not value:
                skipped += 1
                continue

            template = match_parameter_template(
                name,
                category_templates=category_templates,
                existing_templates=existing_templates,
            )

            if not template:
                template = create_parameter_template(
                    name,
                    source_name=raw_name,
                    content_type=content_type,
                )
                if template:
                    existing_templates.append(template)

            if not template:
                skipped += 1
                continue

            note = f"Imported from {source.supplier.upper()} supplier data"
            parameter = Parameter.objects.filter(
                model_type=content_type,
                model_id=part.pk,
                template=template,
            ).first()

            if not parameter:
                parameter = Parameter(
                    model_type=content_type,
                    model_id=part.pk,
                    template=template,
                )

            parameter.data = value[:500]
            parameter.note = note[:500]
            parameter.save(updated_by=user)
            synced += 1

        return synced, skipped

    def ensure_manufacturer_part(self, source: SupplierPartSource, part):
        """Create or update a ManufacturerPart if possible."""
        if not source.mpn:
            return None

        from company.models import Company, ManufacturerPart

        manufacturer = None
        if source.manufacturer:
            manufacturer = Company.objects.filter(name__iexact=source.manufacturer).first()
            if manufacturer and not manufacturer.is_manufacturer:
                manufacturer.is_manufacturer = True
                manufacturer.save()
            if not manufacturer:
                manufacturer = Company(
                    name=source.manufacturer,
                    is_manufacturer=True,
                    is_supplier=False,
                )
                manufacturer.save()

        manufacturer_part = ManufacturerPart.objects.filter(
            part=part,
            manufacturer=manufacturer,
            MPN__iexact=source.mpn,
        ).first()

        if manufacturer_part:
            return manufacturer_part

        manufacturer_part = ManufacturerPart(
            part=part,
            manufacturer=manufacturer,
            MPN=source.mpn,
            description=source.description[:250],
            link=source.datasheet_url[:2000] or source.supplier_link[:2000],
        )
        manufacturer_part.save()
        return manufacturer_part

    def ensure_supplier_part(self, source, part, supplier, manufacturer_part):
        """Create or update a SupplierPart."""
        from company.models import SupplierPart

        supplier_part = SupplierPart.objects.filter(
            part=part,
            supplier=supplier,
            SKU__iexact=source.sku,
        ).first()

        if not supplier_part:
            supplier_part = SupplierPart(
                part=part,
                supplier=supplier,
                SKU=source.sku,
            )

        supplier_part.manufacturer_part = manufacturer_part
        supplier_part.description = source.description[:250]
        supplier_part.link = source.supplier_link[:2000]
        supplier_part.packaging = source.packaging[:50]

        if not part.default_supplier:
            supplier_part.primary = True

        supplier_part.save()
        return supplier_part

    def sync_price_breaks(self, supplier_part, source: SupplierPartSource):
        """Replace supplier price breaks with fresh supplier data."""
        if not source.price_breaks:
            return

        from company.models import SupplierPriceBreak

        SupplierPriceBreak.objects.filter(part=supplier_part).delete()
        SupplierPriceBreak.objects.bulk_create(
            [
                SupplierPriceBreak(
                    part=supplier_part,
                    quantity=quantity,
                    price=price,
                    price_currency=currency,
                )
                for quantity, (price, currency) in source.price_breaks.items()
            ]
        )

    def create_stock_item(
        self,
        barcode: str,
        parsed: ParsedBarcode,
        source: SupplierPartSource,
        supplier_part,
        *,
        location_id: int | None,
        quantity,
        user,
        allow_duplicate: bool,
    ):
        """Create a new stock item for a confirmed scan."""
        from InvenTree.helpers import hash_barcode
        from stock.models import StockItem

        location = self.location_by_id(location_id) or self.default_location(create=True)
        stock_quantity = coerce_quantity(quantity) or parsed.quantity or Decimal("1")
        if stock_quantity <= 0:
            raise SupplierScanError("Quantity must be greater than zero")

        stock_item = StockItem(
            part=supplier_part.part,
            supplier_part=supplier_part,
            quantity=stock_quantity,
            location=location,
            packaging=source.packaging[:50],
            notes=stock_notes(parsed, source),
        )
        stock_item.save(
            user=user,
            notes=f"Received by supplier scan: {parsed.supplier_name} {parsed.sku}",
        )

        assigned = stock_item.assign_barcode(
            barcode_hash=hash_barcode(barcode),
            barcode_data=barcode[:500],
            raise_error=False,
            save=True,
        )

        if not assigned and not allow_duplicate:
            raise DuplicateBarcodeError(self.find_duplicate_stock_item(barcode))

        return stock_item

    def location_by_id(self, location_id):
        """Return a stock location by ID."""
        if not location_id:
            return None

        from stock.models import StockLocation

        return StockLocation.objects.filter(pk=location_id).first()

    def default_location(self, *, create: bool):
        """Return the configured or fallback stock location."""
        from stock.models import StockLocation

        configured = self.setting("DEFAULT_LOCATION", None)
        if configured:
            location = StockLocation.objects.filter(pk=configured).first()
            if location:
                return location

        path = str(self.setting("DEFAULT_LOCATION_PATH", "Stores/Incoming"))
        parts = [p.strip() for p in path.split("/") if p.strip()]
        if not parts:
            return None

        parent = None
        current = None
        for part in parts:
            query = StockLocation.objects.filter(name__iexact=part, parent=parent)
            current = query.first()
            if not current:
                if not create:
                    return None
                current = StockLocation(name=part, parent=parent)
                current.save()
            parent = current

        return current


def object_summary(obj) -> dict[str, Any] | None:
    """Return a compact model summary."""
    if not obj:
        return None

    summary = {"pk": obj.pk, "label": str(obj)}

    if hasattr(obj, "get_absolute_url"):
        try:
            summary["web_url"] = obj.get_absolute_url()
        except Exception:
            pass

    if hasattr(obj, "pathstring"):
        summary["path"] = obj.pathstring

    return summary


def part_summary(part, *, reason: str = "") -> dict[str, Any]:
    """Return a compact part summary for selection controls."""
    summary = object_summary(part) or {}
    summary.update(
        {
            "name": getattr(part, "name", ""),
            "ipn": getattr(part, "IPN", ""),
            "description": getattr(part, "description", ""),
            "category": getattr(part.category, "pathstring", "") if part.category else "",
            "reason": reason,
        }
    )
    return summary


def coerce_quantity(value) -> Decimal | None:
    """Coerce user quantity input."""
    if value in (None, ""):
        return None

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SupplierScanError("Invalid quantity") from exc


def unique_part_match(part_model, field: str, value: str):
    """Return a unique Part match for a field/value pair."""
    if not value:
        return None

    matches = part_model.objects.filter(**{f"{field}__iexact": value})
    if matches.count() == 1:
        return matches.first()

    return None


CATEGORY_ALIASES = {
    "battery product accessory": "Battery Holder",
    "battery accessory": "Battery Holder",
    "battery holder": "Battery Holder",
    "battery contact": "Battery Holder",
    "coin cell holder": "Battery Holder",
    "ceramic capacitor": "Capacitor",
    "aluminum electrolytic capacitor": "Capacitor",
    "electrolytic capacitor": "Capacitor",
    "film capacitor": "Capacitor",
    "tantalum capacitor": "Capacitor",
    "supercapacitor": "Super Capacitor",
    "super capacitor": "Super Capacitor",
    "chip resistor surface mount": "Resistor",
    "resistor network array": "Resistor",
    "inductor coil choke": "Inductor",
    "led indication discrete": "LED",
    "light emitting diode": "LED",
    "rectifier": "Diode",
    "zener diode": "Diode",
    "schottky diode": "Diode",
    "fet mosfet": "Transistor",
    "single fet mosfet": "Transistor",
    "mosfet": "Transistor",
    "bjt transistor": "Transistor",
    "discrete semiconductor product": "Transistor",
    "crystal oscillator resonator": "Crystals",
    "crystal": "Crystals",
    "oscillator": "Crystals",
    "resonator": "Crystals",
    "integrated circuit": "IC",
    "logic gate inverter": "IC",
    "power management pmic": "IC",
    "linear regulator": "Regulator",
    "voltage regulator": "Regulator",
    "dc dc converter": "Converter",
    "terminal block": "Terminal",
    "header connector": "Connector",
    "rectangular connector": "Connector",
    "circular connector": "Connector",
    "rf antenna": "Antenna",
    "rf transceiver": "Transceiver",
    "rfid transponder tag": "RFID",
    "switch tactile": "Switch",
    "pushbutton switch": "Switch",
}

CATEGORY_KEYWORDS = {
    "Battery Holder": [
        "battery holder",
        "battery product accessory",
        "battery contact",
        "coin cell",
    ],
    "Capacitor": ["capacitor", "capacitance", "mlcc"],
    "Resistor": ["resistor", "resistance", "ohm"],
    "Inductor": ["inductor", "inductance", "choke"],
    "Diode": ["diode", "rectifier", "zener", "schottky"],
    "Transistor": ["transistor", "mosfet", "fet", "bjt"],
    "IC": ["integrated circuit", "logic ic", "pmic"],
    "LED": ["led", "light emitting diode"],
    "Connector": ["connector", "header"],
    "Terminal": ["terminal block", "terminal"],
    "Switch": ["switch", "pushbutton", "tactile"],
    "Relay": ["relay"],
    "Fuse": ["fuse"],
    "Regulator": ["regulator", "ldo"],
    "Converter": ["converter", "dc dc"],
    "Antenna": ["antenna"],
    "Crystals": ["crystal", "oscillator", "resonator"],
    "Transceiver": ["transceiver"],
    "Microcontroller": ["microcontroller", "mcu"],
    "Memory Chips": ["memory", "eeprom", "flash", "sram", "dram"],
}

CATEGORY_STOPWORDS = {
    "and",
    "or",
    "the",
    "of",
    "for",
    "with",
    "product",
    "part",
    "component",
    "device",
    "accessory",
    "accessorie",
    "passive",
    "active",
    "semiconductor",
    "electronic",
    "electromechanical",
}

CATEGORY_PLURAL_EXCEPTIONS = {"ac", "dc", "gps", "rfid", "usb"}


def category_index(categories) -> dict[str, list[Any]]:
    """Index categories by normalized name and path."""
    indexed: dict[str, list[Any]] = {}
    for category in categories:
        for value in (category.name, getattr(category, "pathstring", "")):
            key = clean_category_text(value)
            if key:
                indexed.setdefault(key, []).append(category)
    return indexed


def category_lookup_keys(value: str) -> list[tuple[str, str]]:
    """Return normalized category lookup keys and confidence labels."""
    key = clean_category_text(value)
    rows = [(key, "exact")]

    if alias := CATEGORY_ALIASES.get(key):
        rows.append((clean_category_text(alias), "alias"))

    tokens = category_tokens(value)
    if len(tokens) == 1:
        token = next(iter(tokens))
        if alias := CATEGORY_ALIASES.get(token):
            rows.append((clean_category_text(alias), "alias"))

    seen = set()
    output = []
    for row in rows:
        if row[0] and row[0] not in seen:
            output.append(row)
            seen.add(row[0])
    return output


def unique_category_for_key(indexed: dict[str, list[Any]], key: str):
    """Return a category if a normalized key identifies exactly one category."""
    matches = indexed.get(key, [])
    unique = {category.pk: category for category in matches}
    if len(unique) == 1:
        return next(iter(unique.values()))
    return None


def best_source_category_match(source: SupplierPartSource, categories) -> tuple[Any, str] | None:
    """Find the best category match from all supplier text."""
    text = clean_category_text(" ".join(category_source_terms(source)))
    if not text:
        return None

    source_tokens = set(text.split()) - CATEGORY_STOPWORDS
    matches = []

    for category in categories:
        category_key = clean_category_text(category.name)
        if not category_key:
            continue

        score = 0
        confidence = "fuzzy"

        if phrase_in_category_text(category_key, text, source_tokens):
            score = 80

        tokens = category_tokens(category.name)
        if tokens and tokens.issubset(source_tokens):
            score = max(score, 70)

        for alias_key, category_name in CATEGORY_ALIASES.items():
            if clean_category_text(category_name) != category_key:
                continue
            if phrase_in_category_text(alias_key, text, source_tokens):
                score = max(score, 85)
                confidence = "alias"

        for category_name, keywords in CATEGORY_KEYWORDS.items():
            if clean_category_text(category_name) != category_key:
                continue
            if any(
                phrase_in_category_text(keyword, text, source_tokens)
                for keyword in keywords
            ):
                score = max(score, 82)
                confidence = "fuzzy"

        if score:
            matches.append((score, category.pk, category, confidence))

    if not matches:
        return None

    matches.sort(key=lambda row: (-row[0], row[2].pathstring))
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        return None

    _, _, category, confidence = matches[0]
    return category, confidence


def category_source_terms(source: SupplierPartSource) -> list[str]:
    """Return supplier fields useful for category matching."""
    terms = [
        *source.category_path,
        source.name,
        source.description,
        source.manufacturer,
        source.packaging,
    ]
    for key, value in source.parameters.items():
        terms.extend([key, value])
    return [str(term) for term in terms if term]


def phrase_in_category_text(phrase: str, text: str, tokens: set[str]) -> bool:
    """Return true when a normalized phrase appears as words in text."""
    phrase = clean_category_text(phrase)
    if not phrase:
        return False

    phrase_tokens = phrase.split()
    if len(phrase_tokens) == 1:
        return phrase_tokens[0] in tokens

    return f" {phrase} " in f" {text} "


def category_tokens(value: str) -> set[str]:
    """Return useful normalized tokens for category matching."""
    return set(clean_category_text(value).split()) - CATEGORY_STOPWORDS


def clean_category_text(value: str) -> str:
    """Return a punctuation- and plural-insensitive category key."""
    text = str(value or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = [singular_category_token(token) for token in text.split()]
    return " ".join(token for token in tokens if token)


def singular_category_token(token: str) -> str:
    """Return a simple singular form for supplier/category words."""
    if token in CATEGORY_PLURAL_EXCEPTIONS:
        return token
    if len(token) > 4 and token.endswith("ies"):
        return f"{token[:-3]}y"
    if len(token) > 4 and (
        token.endswith("ches") or token.endswith("shes") or token.endswith("xes")
    ):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


PARAMETER_ALIASES = {
    "capacitance": "Capacitance",
    "capacity": "Capacitance",
    "resistance": "Resistance",
    "impedance": "Impedance",
    "tolerance": "Tolerance",
    "voltage": "Voltage Rating",
    "voltage rated": "Voltage Rating",
    "rated voltage": "Voltage Rating",
    "operating voltage": "Operating Voltage",
    "current": "Current Rating",
    "current rating": "Current Rating",
    "rated current": "Current Rating",
    "power": "Power Rating",
    "power rating": "Power Rating",
    "package": "Package",
    "package case": "Package",
    "case package": "Package",
    "package type": "Package",
    "supplier device package": "Supplier Device Package",
    "mounting type": "Mounting Type",
    "mounting style": "Mounting Type",
    "operating temperature": "Operating Temperature",
    "temperature range": "Operating Temperature",
    "temperature coefficient": "Temperature Coefficient",
    "dielectric": "Dielectric",
    "series": "Series",
    "size dimension": "Size",
    "length": "Length",
    "width": "Width",
    "height": "Height",
    "lifecycle": "Lifecycle Status",
    "rohs": "RoHS",
}


def normalize_supplier_image_url(url: str, supplier: str) -> str:
    """Return an absolute image URL."""
    url = str(url or "").strip()
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        if supplier == "lcsc":
            return f"https://www.lcsc.com{url}"
        if supplier == "digikey":
            return f"https://www.digikey.com{url}"
    return url


def image_extension(url: str, content_type: str) -> str:
    """Infer a file extension for a downloaded image."""
    content_types = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
    }
    if extension := content_types.get(content_type):
        return extension

    path = urlparse(url).path.lower()
    for extension in ("jpg", "jpeg", "png", "gif", "webp"):
        if path.endswith(f".{extension}"):
            return "jpg" if extension == "jpeg" else extension

    return "jpg"


def safe_filename(value: str) -> str:
    """Return a filesystem-safe filename."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "part_image"))
    return re.sub(r"_+", "_", name).strip("._") or "part_image.jpg"


def clean_parameter_name(value: Any) -> str:
    """Normalize a supplier parameter name for InvenTree."""
    name = re.sub(r"<[^>]+>", "", str(value or ""))
    name = re.sub(r"\s+", " ", name.replace("_", " ").replace("/", " / ")).strip(" :-")
    if not name:
        return ""

    alias = PARAMETER_ALIASES.get(normalize_parameter_name(name))
    return (alias or name)[:100]


def clean_parameter_value(value: Any) -> str:
    """Normalize a supplier parameter value for InvenTree."""
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if text.lower() in {"", "-", "--", "n/a", "na", "null", "none"}:
        return ""
    return text


def normalize_parameter_name(value: str) -> str:
    """Return a punctuation-insensitive parameter name."""
    value = str(value or "").lower()
    value = re.sub(r"[\[\]()/,_:-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def parameter_name_candidates(name: str) -> list[str]:
    """Return possible canonical names for a supplier parameter."""
    normalized = normalize_parameter_name(name)
    candidates = [name]
    if alias := PARAMETER_ALIASES.get(normalized):
        candidates.append(alias)
    return dedupe_strings([candidate[:100] for candidate in candidates if candidate])


def match_parameter_template(
    name: str,
    *,
    category_templates,
    existing_templates,
):
    """Find the best existing ParameterTemplate for a supplier parameter."""
    candidate_names = parameter_name_candidates(name)
    candidate_keys = {normalize_parameter_name(candidate) for candidate in candidate_names}

    for category_template in category_templates:
        template = getattr(category_template, "template", None)
        if template and normalize_parameter_name(template.name) in candidate_keys:
            return template

    for template in existing_templates:
        if normalize_parameter_name(template.name) in candidate_keys:
            return template

    return None


def create_parameter_template(name: str, *, source_name: str, content_type):
    """Create a ParameterTemplate for a supplier parameter if one does not exist."""
    from django.db import IntegrityError
    from common.models import ParameterTemplate

    for candidate in parameter_name_candidates(name):
        template = ParameterTemplate.objects.filter(name__iexact=candidate).first()
        if template:
            return template

    template_name = parameter_name_candidates(name)[0][:100]
    description = f"Imported from supplier parameter '{source_name}'"[:250]

    try:
        template = ParameterTemplate(
            name=template_name,
            description=description,
            model_type=content_type,
            units="",
            enabled=True,
        )
        template.save()
        return template
    except IntegrityError:
        return ParameterTemplate.objects.filter(name__iexact=template_name).first()


def dedupe_strings(values: list[str]) -> list[str]:
    """Deduplicate strings while preserving order."""
    seen = set()
    output = []
    for value in values:
        if value and value not in seen:
            output.append(value)
            seen.add(value)
    return output


def source_notes(source: SupplierPartSource) -> str:
    """Build part notes from supplier metadata."""
    lines = [
        "Created by supplier scan.",
        "",
        f"Supplier SKU: {source.sku}",
        f"Manufacturer Part Number: {source.mpn}",
    ]
    if source.supplier_link:
        lines.append(f"Supplier link: {source.supplier_link}")
    if source.datasheet_url:
        lines.append(f"Datasheet: {source.datasheet_url}")
    if source.parameters:
        lines.extend(["", "Supplier parameters:"])
        for key, value in sorted(source.parameters.items()):
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def stock_notes(parsed: ParsedBarcode, source: SupplierPartSource) -> str:
    """Build stock item notes from scan metadata."""
    lines = [
        "Received by supplier scan.",
        "",
        f"Supplier: {parsed.supplier_name}",
        f"Supplier SKU: {parsed.sku}",
    ]
    if parsed.mpn:
        lines.append(f"MPN: {parsed.mpn}")
    if parsed.order_number:
        lines.append(f"Order: {parsed.order_number}")
    if source.packaging:
        lines.append(f"Packaging: {source.packaging}")
    if parsed.raw_fields:
        lines.extend(["", "Barcode fields:"])
        for key, value in parsed.raw_fields.items():
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)
