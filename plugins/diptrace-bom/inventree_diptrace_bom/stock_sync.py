"""Mirror JLCPCB-owned component inventory into managed InvenTree stock items."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from .jlcpcb import JlcClient, JlcCredentials, private_stock_quantities


class JlcStockSyncError(RuntimeError):
    """Raised when stock synchronization cannot run safely."""


MANAGED_BATCH_PREFIX = "DIPTRACE-JLC:"

BUCKETS = {
    "consigned": {
        "setting": "JLC_CONSIGNED_LOCATION",
        "label": "Consigned Parts",
    },
    "private": {
        "setting": "JLC_PRIVATE_LOCATION",
        "label": "JLCPCB Private Parts",
    },
    "global": {
        "setting": "JLC_GLOBAL_LOCATION",
        "label": "Global Sourcing Reserved",
    },
}


def normalize_code(value: Any) -> str:
    """Normalize a supplier SKU or API component code for exact matching."""
    return str(value or "").strip().upper()


def managed_batch(bucket: str, component_code: str) -> str:
    """Return the stable batch marker used to identify one managed stock item."""
    return f"{MANAGED_BATCH_PREFIX}{bucket}:{normalize_code(component_code)}"


def parse_managed_batch(value: Any) -> tuple[str, str] | None:
    """Parse a plugin-managed batch marker, or return ``None`` for normal stock."""
    text = str(value or "")
    if not text.startswith(MANAGED_BATCH_PREFIX):
        return None
    remainder = text[len(MANAGED_BATCH_PREFIX) :]
    bucket, separator, code = remainder.partition(":")
    code = normalize_code(code)
    if not separator or bucket not in BUCKETS or not code:
        return None
    return bucket, code


class JlcStockSyncService:
    """Reconcile successful JLC private-library snapshots with external stock."""

    def __init__(self, plugin):
        self.plugin = plugin

    def sync(self, *, user=None) -> dict:
        """Fetch a complete snapshot, then reconcile only plugin-managed rows."""
        supplier_id = self.setting("JLC_SUPPLIER")
        if not supplier_id:
            raise JlcStockSyncError("Configure the JLC / LCSC Supplier before syncing stock")

        credentials = self.credentials()
        if not credentials.configured:
            raise JlcStockSyncError(
                "Configure the JLCPCB App ID, Access Key and Secret Key before syncing stock"
            )

        locations = self.locations()
        client = JlcClient(
            credentials,
            host=str(self.setting("JLC_HOST", "https://open.jlcpcb.com")),
            timeout=int(self.setting("JLC_TIMEOUT", 30)),
        )

        # Do not enter a database transaction until a complete API snapshot succeeds.
        library = client.private_library()
        return self._reconcile(
            supplier_id=supplier_id,
            locations=locations,
            library=library,
            user=user,
        )

    def locations(self) -> dict[str, Any]:
        """Return and validate the three configured external stock locations."""
        from stock.models import StockLocation

        location_ids = {
            bucket: self.setting(config["setting"])
            for bucket, config in BUCKETS.items()
        }
        missing = [BUCKETS[bucket]["label"] for bucket, value in location_ids.items() if not value]
        if missing:
            raise JlcStockSyncError(
                "Configure all JLCPCB stock locations: " + ", ".join(missing)
            )

        if len({str(value) for value in location_ids.values()}) != len(BUCKETS):
            raise JlcStockSyncError("Each JLCPCB inventory bucket must use a different location")

        records = {
            str(location.pk): location
            for location in StockLocation.objects.filter(pk__in=location_ids.values())
        }
        locations = {
            bucket: records.get(str(location_id))
            for bucket, location_id in location_ids.items()
        }
        invalid = [BUCKETS[bucket]["label"] for bucket, location in locations.items() if not location]
        if invalid:
            raise JlcStockSyncError("Configured stock location no longer exists: " + ", ".join(invalid))

        structural = [location.pathstring for location in locations.values() if location.structural]
        if structural:
            raise JlcStockSyncError(
                "Stock cannot be placed directly in structural locations: " + ", ".join(structural)
            )

        internal = [location.pathstring for location in locations.values() if not location.external]
        if internal:
            raise JlcStockSyncError(
                "Mark every JLCPCB stock location as External before syncing: "
                + ", ".join(internal)
            )
        return locations

    def _reconcile(self, *, supplier_id, locations: dict[str, Any], library: dict, user) -> dict:
        """Apply a complete successful snapshot in one database transaction."""
        from company.models import Company, SupplierPart
        from django.db import transaction
        from stock.models import StockItem

        normalized_library = {
            normalize_code(code): item
            for code, item in library.items()
            if normalize_code(code)
        }
        result = {
            "api_components": len(normalized_library),
            "matched_components": 0,
            "created": 0,
            "updated": 0,
            "zeroed": 0,
            "unchanged": 0,
            "unmatched_components": 0,
            "ambiguous_components": 0,
            "unmatched_codes": [],
            "ambiguous_codes": [],
        }

        with transaction.atomic():
            supplier = (
                Company.objects.select_for_update()
                .filter(pk=supplier_id, is_supplier=True)
                .first()
            )
            if not supplier:
                raise JlcStockSyncError("The configured JLC / LCSC supplier no longer exists")

            supplier_parts_by_code: dict[str, list[Any]] = defaultdict(list)
            supplier_parts = list(
                SupplierPart.objects.select_for_update()
                .filter(supplier=supplier)
                .select_related("part")
                .order_by("pk")
            )
            for supplier_part in supplier_parts:
                code = normalize_code(supplier_part.SKU)
                if code:
                    supplier_parts_by_code[code].append(supplier_part)

            location_ids = [location.pk for location in locations.values()]
            managed_items = list(
                StockItem.objects.select_for_update()
                .filter(
                    location_id__in=location_ids,
                    batch__startswith=MANAGED_BATCH_PREFIX,
                )
                .select_related("supplier_part", "part")
                .order_by("pk")
            )
            managed_by_key: dict[tuple[str, str], list[Any]] = defaultdict(list)
            for stock_item in managed_items:
                parsed = parse_managed_batch(stock_item.batch)
                if not parsed:
                    continue
                bucket, code = parsed
                if stock_item.location_id != locations[bucket].pk:
                    continue
                managed_by_key[(code, bucket)].append(stock_item)

            expected_keys: set[tuple[str, str]] = set()
            ambiguous_codes: set[str] = set()

            for code, private_item in normalized_library.items():
                matches = supplier_parts_by_code.get(code, [])
                if not matches:
                    result["unmatched_components"] += 1
                    if len(result["unmatched_codes"]) < 25:
                        result["unmatched_codes"].append(code)
                    continue
                if len(matches) != 1:
                    ambiguous_codes.add(code)
                    result["ambiguous_components"] += 1
                    if len(result["ambiguous_codes"]) < 25:
                        result["ambiguous_codes"].append(code)
                    continue

                result["matched_components"] += 1
                supplier_part = matches[0]
                quantities = private_stock_quantities(private_item)
                for bucket, desired in quantities.items():
                    key = (code, bucket)
                    expected_keys.add(key)
                    candidates = [
                        stock_item
                        for stock_item in managed_by_key.get(key, [])
                        if stock_item.supplier_part_id == supplier_part.pk
                        and stock_item.part_id == supplier_part.part_id
                    ]
                    canonical = candidates[0] if candidates else None

                    if canonical is None:
                        if desired > 0:
                            canonical = StockItem(
                                part=supplier_part.part,
                                supplier_part=supplier_part,
                                location=locations[bucket],
                                quantity=desired,
                                batch=managed_batch(bucket, code),
                                delete_on_deplete=False,
                            )
                            canonical.save(
                                user=user,
                                notes=f"Created from JLCPCB {BUCKETS[bucket]['label']} sync",
                            )
                            result["created"] += 1
                    elif canonical.quantity != desired:
                        previous = canonical.quantity
                        canonical.stocktake(
                            desired,
                            user,
                            notes=f"JLCPCB {BUCKETS[bucket]['label']} sync",
                        )
                        result["updated"] += 1
                        if previous > 0 and desired == 0:
                            result["zeroed"] += 1
                    else:
                        result["unchanged"] += 1

                    # Older, concurrent, or previously mis-linked rows must not
                    # double-count availability. Preserve them for audit, at zero.
                    for duplicate in managed_by_key.get(key, []):
                        if canonical is not None and duplicate.pk == canonical.pk:
                            continue
                        if duplicate.quantity != Decimal("0"):
                            duplicate.stocktake(
                                Decimal("0"),
                                user,
                                notes="Duplicate DipTrace JLCPCB sync row zeroed",
                            )
                            result["updated"] += 1
                            result["zeroed"] += 1

            # A complete successful snapshot is authoritative. Managed codes no longer
            # returned by JLCPCB are set to zero; ordinary InvenTree stock is untouched.
            for key, stock_items in managed_by_key.items():
                code, _bucket = key
                if key in expected_keys or code in ambiguous_codes:
                    continue
                for stock_item in stock_items:
                    if stock_item.quantity != Decimal("0"):
                        stock_item.stocktake(
                            Decimal("0"),
                            user,
                            notes="JLCPCB component absent from successful private-library snapshot",
                        )
                        result["updated"] += 1
                        result["zeroed"] += 1

        result["changed"] = result["created"] + result["updated"]
        return result

    def credentials(self) -> JlcCredentials:
        """Return protected JLCPCB credentials from plugin settings."""
        return JlcCredentials(
            app_id=str(self.setting("JLC_APP_ID", "")),
            access_key=str(self.setting("JLC_ACCESS_KEY", "")),
            tokenization_key=str(self.setting("JLC_TOKENIZATION_KEY", "")),
        )

    def setting(self, key: str, default=None):
        """Read a fresh setting value so workers see configuration changes."""
        try:
            value = self.plugin.get_setting(key, cache=False)
        except Exception:
            return default
        return default if value in (None, "") else value
