"""InvenTree plugin entrypoint for supplier scanning."""

from __future__ import annotations

import json
from urllib.parse import quote

from django.http import HttpResponseForbidden, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.urls import path
from django.utils.translation import gettext_lazy as _

from plugin import InvenTreePlugin
from plugin.mixins import (
    BarcodeMixin,
    NavigationMixin,
    SettingsMixin,
    UrlsMixin,
    UserInterfaceMixin,
)

from .barcodes import BarcodeParseError, parse_barcode
from .services import (
    DuplicateBarcodeError,
    SupplierScanError,
    SupplierScanService,
    object_summary,
)
from .suppliers import lean_source

__all__ = []


class SupplierScanPlugin(
    SettingsMixin,
    UrlsMixin,
    NavigationMixin,
    UserInterfaceMixin,
    BarcodeMixin,
    InvenTreePlugin,
):
    """Scan supplier barcodes and receive stock."""

    NAME = "SupplierScanPlugin"
    SLUG = "supplier-scan"
    TITLE = "Supplier Scan"
    DESCRIPTION = "Scan LCSC/JLC and DigiKey supplier barcodes to create parts and receive stock"
    VERSION = "0.1.0"
    AUTHOR = "Corrosion Instruments"
    MIN_VERSION = "1.3.0"

    NAVIGATION_TAB_NAME = "Supplier Scan"
    NAVIGATION_TAB_ICON = "fas fa-barcode"
    NAVIGATION = [{"name": "Supplier Scan", "link": "plugin:supplier-scan:index"}]

    SETTINGS = {
        "AUTO_CREATE": {
            "name": _("Auto Create"),
            "description": _(
                "Automatically receive a scan when the part exists or the category match is confident"
            ),
            "default": False,
            "validator": bool,
        },
        "BLOCK_DUPLICATE_BARCODES": {
            "name": _("Block Duplicate Barcodes"),
            "description": _("Block the exact same supplier barcode from being received twice"),
            "default": True,
            "validator": bool,
        },
        "DEFAULT_LOCATION": {
            "name": _("Default Location"),
            "description": _("Default receiving location for scanned stock"),
            "model": "stock.stocklocation",
        },
        "DEFAULT_LOCATION_PATH": {
            "name": _("Default Location Path"),
            "description": _("Fallback location path to use/create when no default location is selected"),
            "default": "Stores/Incoming",
        },
        "LCSC_SUPPLIER": {
            "name": _("LCSC Supplier"),
            "description": _("InvenTree supplier company used for LCSC/JLC scans"),
            "model": "company.company",
            "model_filters": {"is_supplier": True},
        },
        "DIGIKEY_SUPPLIER": {
            "name": _("DigiKey Supplier"),
            "description": _("InvenTree supplier company used for DigiKey scans"),
            "model": "company.company",
            "model_filters": {"is_supplier": True},
        },
        "DIGIKEY_CLIENT_ID": {
            "name": _("DigiKey Client ID"),
            "description": _("Optional DigiKey API client ID for rich part import"),
            "default": "",
        },
        "DIGIKEY_CLIENT_SECRET": {
            "name": _("DigiKey Client Secret"),
            "description": _("Optional DigiKey API client secret for rich part import"),
            "default": "",
            "protected": True,
        },
        "DIGIKEY_CURRENCY": {
            "name": _("DigiKey Currency"),
            "description": _("ISO currency code for DigiKey API calls"),
            "default": "AUD",
        },
        "DIGIKEY_LANGUAGE": {
            "name": _("DigiKey Language"),
            "description": _("DigiKey API language code"),
            "default": "en",
        },
        "DIGIKEY_LOCATION": {
            "name": _("DigiKey Location"),
            "description": _("DigiKey API location/site code"),
            "default": "AU",
        },
    }

    def index(self, request):
        """Render the scanner page."""
        if not request.user.is_authenticated:
            return HttpResponseForbidden("Authentication required")

        return render(
            request,
            "inventree_supplier_scan/scan.html",
            {
                "title": self.TITLE,
                "csrf_token": get_token(request),
            },
        )

    def preview(self, request):
        """Preview a barcode scan."""
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=403)

        try:
            data = json_request(request)
            response = SupplierScanService(self).preview(data.get("barcode", ""))
            return JsonResponse(response)
        except SupplierScanError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Exception as exc:
            return JsonResponse({"error": f"Preview failed: {exc}"}, status=500)

    def receive(self, request):
        """Receive a confirmed barcode scan."""
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=403)

        from stock.models import StockItem
        from users.permissions import check_user_permission

        if not check_user_permission(request.user, StockItem, "add"):
            return JsonResponse({"error": "Missing stock add permission"}, status=403)

        try:
            data = json_request(request)
            response = SupplierScanService(self).receive(
                data.get("barcode", ""),
                user=request.user,
                category_id=data.get("category_id"),
                location_id=data.get("location_id"),
                quantity=data.get("quantity"),
                allow_duplicate=bool(data.get("allow_duplicate", False)),
                part_id=data.get("part_id"),
                use_supplier_image=bool(data.get("use_supplier_image", True)),
            )
            return JsonResponse(response)
        except DuplicateBarcodeError as exc:
            return JsonResponse(
                {
                    "error": str(exc),
                    "duplicate_stock_item": object_summary(exc.stock_item),
                    "duplicate": True,
                },
                status=409,
            )
        except SupplierScanError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Exception as exc:
            return JsonResponse({"error": f"Receive failed: {exc}"}, status=500)

    def categories(self, request):
        """Return selectable part categories."""
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=403)

        from part.models import PartCategory

        categories = [
            {
                "pk": category.pk,
                "name": category.name,
                "path": category.pathstring,
                "structural": category.structural,
            }
            for category in PartCategory.objects.all().order_by("tree_id", "lft")
        ]

        return JsonResponse({"categories": categories})

    def locations(self, request):
        """Return selectable stock locations."""
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=403)

        from stock.models import StockLocation

        locations = [
            {
                "pk": location.pk,
                "name": location.name,
                "path": location.pathstring,
                "structural": location.structural,
            }
            for location in StockLocation.objects.all().order_by("tree_id", "lft")
        ]

        return JsonResponse({"locations": locations})

    def parts(self, request):
        """Search existing parts for manual selection."""
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=403)

        rows = SupplierScanService(self).search_parts(request.GET.get("q", ""))
        return JsonResponse({"parts": rows})

    def scan(self, barcode_data):
        """Recognize supplier barcodes from InvenTree's global barcode scanner."""
        try:
            parsed = parse_barcode(barcode_data)
        except BarcodeParseError:
            return None

        service = SupplierScanService(self)
        scan_url = f"/plugin/{self.SLUG}/?barcode={quote(str(barcode_data), safe='')}"
        result = {
            "supplier_scan": {
                "url": scan_url,
                "supplier": parsed.supplier_name,
                "sku": parsed.sku,
                "mpn": parsed.mpn,
                "quantity": str(parsed.quantity) if parsed.quantity is not None else "",
            },
            "message": "Supplier barcode recognized",
        }

        supplier_part = service.find_supplier_part(parsed)
        if supplier_part:
            result["supplierpart"] = supplier_part.format_matched_response()
            if getattr(supplier_part, "part", None):
                result["part"] = supplier_part.part.format_matched_response()
            return result

        source = lean_source(parsed)
        part = service.find_existing_part(source)
        if part:
            result["part"] = part.format_matched_response()

        return result

    def get_ui_dashboard_items(self, request, context, **kwargs):
        """Return dashboard entry point for supplier scanning."""
        return [
            {
                "key": "supplier-scan-dashboard",
                "title": _("Supplier Scan"),
                "description": _("Scan LCSC/JLC and DigiKey labels into stock"),
                "icon": "ti:barcode",
                "source": self.plugin_static_file("supplier_scan_dashboard.js"),
                "options": {"width": 3, "height": 2},
                "context": {"url": f"/plugin/{self.SLUG}/"},
            }
        ]

    def get_ui_navigation_items(self, request, context, **kwargs):
        """Return navigation entry point for the modern InvenTree UI."""
        # The modern UI treats custom navigation URLs as SPA routes under /web.
        # Keep Supplier Scan available from the dashboard widget and legacy nav.
        return []

    def setup_urls(self):
        """Setup plugin URLs."""
        return [
            path("", self.index, name="index"),
            path("preview/", self.preview, name="preview"),
            path("receive/", self.receive, name="receive"),
            path("categories/", self.categories, name="categories"),
            path("locations/", self.locations, name="locations"),
            path("parts/", self.parts, name="parts"),
        ]


def json_request(request) -> dict:
    """Read a JSON request body."""
    if request.method != "POST":
        raise SupplierScanError("POST required")

    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise SupplierScanError("Invalid JSON request") from exc
