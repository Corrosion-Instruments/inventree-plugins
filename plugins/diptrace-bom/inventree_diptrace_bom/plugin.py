"""InvenTree plugin entry point for DipTrace BOM import."""

from __future__ import annotations

import json
import logging

from django.core import signing
from django.core.exceptions import ValidationError
from django.http import HttpResponseForbidden, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.urls import path
from django.utils.translation import gettext_lazy as _

from plugin import InvenTreePlugin
from plugin.mixins import (
    NavigationMixin,
    ScheduleMixin,
    SettingsMixin,
    UrlsMixin,
    UserInterfaceMixin,
)

from .jlcpcb import JlcApiError, JlcClient
from .parser import BomParseError, parse_bom
from .services import BomImportError, BomImportService
from .stock_sync import JlcStockSyncError, JlcStockSyncService

__all__ = []
logger = logging.getLogger(__name__)


class DipTraceBomPlugin(
    ScheduleMixin,
    SettingsMixin,
    UrlsMixin,
    NavigationMixin,
    UserInterfaceMixin,
    InvenTreePlugin,
):
    """Upload DipTrace BOMs, check stock, and finalize InvenTree BOMs."""

    NAME = "DipTraceBomPlugin"
    SLUG = "diptrace-bom"
    TITLE = "DipTrace BOM"
    DESCRIPTION = "Import DipTrace BOMs and synchronize InvenTree / JLCPCB availability"
    VERSION = "0.3.26"
    AUTHOR = "Corrosion Instruments"
    MIN_VERSION = "1.5.2"

    NAVIGATION_TAB_NAME = "DipTrace BOM"
    NAVIGATION_TAB_ICON = "fas fa-list-check"
    NAVIGATION = [{"name": "DipTrace BOM", "link": "plugin:diptrace-bom:index"}]

    SCHEDULED_TASKS = {
        "jlc-stock-sync": {
            "func": "sync_jlc_stock",
            "schedule": "I",
            "minutes": 30,
        }
    }

    SETTINGS = {
        "JLC_APP_ID": {
            "name": _("JLCPCB App ID"),
            "description": _("App ID from the JLCPCB Open Platform application"),
            "default": "",
            "protected": True,
        },
        "JLC_ACCESS_KEY": {
            "name": _("JLCPCB Access Key"),
            "description": _("Access key from the JLCPCB Open Platform application"),
            "default": "",
            "protected": True,
        },
        "JLC_TOKENIZATION_KEY": {
            "name": _("JLCPCB Secret Key"),
            "description": _("Secret key from the JLCPCB API key pair (not an RSA private key)"),
            "default": "",
            "protected": True,
        },
        "JLC_HOST": {
            "name": _("JLCPCB API Host"),
            "description": _("Official JLCPCB Open API host"),
            "default": "https://open.jlcpcb.com",
        },
        "JLC_TIMEOUT": {
            "name": _("JLCPCB Request Timeout"),
            "description": _("Maximum seconds to wait for a JLCPCB API response"),
            "default": 30,
            "validator": int,
        },
        "JLC_SUPPLIER": {
            "name": _("JLC / LCSC Supplier"),
            "description": _("Supplier company whose SKU is the DipTrace JLCPCB Part #"),
            "model": "company.company",
            "model_filters": {"is_supplier": True},
        },
        "ENABLE_JLC_STOCK_SYNC": {
            "name": _("Enable JLCPCB Stock Sync"),
            "description": _(
                "Every 30 minutes, mirror JLCPCB-owned quantities onto exact existing supplier-SKU matches"
            ),
            "default": False,
            "validator": bool,
        },
        "JLC_CONSIGNED_LOCATION": {
            "name": _("JLCPCB Consigned Parts Location"),
            "description": _("External, non-structural location for consignedParts"),
            "model": "stock.stocklocation",
        },
        "JLC_PRIVATE_LOCATION": {
            "name": _("JLCPCB Private Parts Location"),
            "description": _("External, non-structural location for jlcpcbParts"),
            "model": "stock.stocklocation",
        },
        "JLC_GLOBAL_LOCATION": {
            "name": _("JLCPCB Global Sourcing Location"),
            "description": _("External, non-structural location for globalSourcingParts"),
            "model": "stock.stocklocation",
        },
        "ALLOW_CREATE_MISSING": {
            "name": _("Allow Missing Part Creation"),
            "description": _("Permit an importer to create unresolved component parts during finalization"),
            "default": False,
            "validator": bool,
        },
        "DEFAULT_COMPONENT_CATEGORY": {
            "name": _("Default Component Category"),
            "description": _("Category used when explicitly creating unresolved BOM components"),
            "model": "part.partcategory",
        },
    }

    def index(self, request):
        if not request.user.is_authenticated:
            return HttpResponseForbidden("Authentication required")
        return render(
            request,
            "inventree_diptrace_bom/import.html",
            {
                "title": self.TITLE,
                "csrf_token": get_token(request),
                "initial_part": request.GET.get("part", ""),
            },
        )

    def preview(self, request):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=403)
        if request.method != "POST":
            return JsonResponse({"error": "POST required"}, status=405)
        uploaded = request.FILES.get("file")
        if not uploaded:
            return JsonResponse({"error": "Select a BOM file"}, status=400)
        if uploaded.size > 10 * 1024 * 1024:
            return JsonResponse({"error": "The BOM file is larger than 10 MB"}, status=400)

        try:
            rows = [row.as_dict() for row in parse_bom(uploaded, uploaded.name)]
            result = BomImportService(self).preview(rows, request.POST.get("assembly_id"))
            result["preview_token"] = signing.dumps(
                {"rows": rows, "filename": uploaded.name},
                salt="inventree-diptrace-bom-preview",
                compress=True,
            )
            result["filename"] = uploaded.name
            return JsonResponse(result)
        except (BomParseError, BomImportError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Exception as exc:
            return JsonResponse({"error": f"Preview failed: {exc}"}, status=500)

    def test_connection(self, request):
        """Test both JLCPCB endpoints without exposing stored credentials."""
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=403)
        if request.method != "POST":
            return JsonResponse({"error": "POST required"}, status=405)

        service = BomImportService(self)
        credentials = service.jlc_credentials()
        configured = {
            "app_id": bool(credentials.app_id),
            "access_key": bool(credentials.access_key),
            "secret_key": bool(credentials.tokenization_key),
        }
        if not credentials.configured:
            return JsonResponse(
                {
                    "ok": False,
                    "configured": configured,
                    "checks": {},
                    "message": "Enter the App ID, Access Key and Secret Key in plugin settings",
                }
            )

        client = JlcClient(
            credentials,
            host=str(service.setting("JLC_HOST", "https://open.jlcpcb.com")),
            timeout=int(service.setting("JLC_TIMEOUT", 30)),
        )
        checks = {}
        probes = (
            ("component_catalogue", lambda: client.component_details(["C25804"])),
            (
                "private_inventory",
                lambda: client.private_library(
                    page_size=1,
                    max_pages=1,
                    require_complete=False,
                ),
            ),
        )
        for name, probe in probes:
            try:
                probe()
                checks[name] = {"ok": True, "message": "Connected"}
            except JlcApiError as exc:
                checks[name] = {"ok": False, "message": str(exc)}

        ok = all(check["ok"] for check in checks.values())
        return JsonResponse(
            {
                "ok": ok,
                "configured": configured,
                "checks": checks,
                "message": "JLCPCB connection successful" if ok else "One or more JLCPCB checks failed",
            }
        )

    def sync_jlc_stock(self, *args, **kwargs):
        """Scheduled entry point for the JLCPCB external-stock reconciliation."""
        try:
            enabled = bool(self.get_setting("ENABLE_JLC_STOCK_SYNC", cache=False))
        except Exception:
            enabled = False
        if not enabled:
            return {"skipped": True, "message": "JLCPCB stock sync is disabled"}
        return JlcStockSyncService(self).sync()

    def sync_stock(self, request):
        """Run the same reconciliation immediately for an authorized user."""
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=403)
        if request.method != "POST":
            return JsonResponse({"error": "POST required"}, status=405)

        from stock.models import StockItem
        from users.permissions import check_user_permission

        for action in ("add", "change"):
            if not check_user_permission(request.user, StockItem, action):
                return JsonResponse({"error": f"Missing stock {action} permission"}, status=403)

        try:
            return JsonResponse(JlcStockSyncService(self).sync(user=request.user))
        except JlcStockSyncError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except JlcApiError as exc:
            return JsonResponse({"error": str(exc)}, status=502)
        except ValidationError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Exception:
            logger.exception("Unexpected JLCPCB stock synchronization failure")
            return JsonResponse(
                {"error": "Stock sync failed unexpectedly; check the InvenTree server log"},
                status=500,
            )

    def finalize(self, request):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=403)
        try:
            data = json_request(request)
            preview = signing.loads(
                data.get("preview_token", ""),
                salt="inventree-diptrace-bom-preview",
                max_age=3600,
            )
        except signing.SignatureExpired:
            return JsonResponse({"error": "The preview expired; upload the BOM again"}, status=400)
        except signing.BadSignature:
            return JsonResponse({"error": "The preview token is invalid"}, status=400)
        except BomImportError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        from company.models import SupplierPart
        from part.models import BomItem, Part
        from users.permissions import check_user_permission

        mode = str(data.get("mode") or "merge")
        required_permissions = [(BomItem, "add"), (BomItem, "change")]
        if mode == "replace":
            required_permissions.append((BomItem, "delete"))
        if bool(data.get("create_missing", False)):
            required_permissions.extend([(Part, "add"), (SupplierPart, "add")])
        for model, action in required_permissions:
            if not check_user_permission(request.user, model, action):
                return JsonResponse({"error": f"Missing {action} permission for {model.__name__}"}, status=403)

        try:
            result = BomImportService(self).finalize(
                preview["rows"],
                assembly_id=int(data.get("assembly_id")),
                selections=data.get("selections") or {},
                mode=mode,
                validate=bool(data.get("validate", False)),
                create_missing=bool(data.get("create_missing", False)),
                user=request.user,
            )
            return JsonResponse(result)
        except (BomImportError, TypeError, ValueError, ValidationError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Exception as exc:
            return JsonResponse({"error": f"Finalization failed: {exc}"}, status=500)

    def parts(self, request):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=403)
        service = BomImportService(self)
        if request.GET.get("assemblies") in {"1", "true"}:
            rows = service.search_assemblies(request.GET.get("q", ""))
        else:
            rows = service.search_parts(request.GET.get("q", ""))
        return JsonResponse({"parts": rows})

    def get_ui_dashboard_items(self, request, context, **kwargs):
        return [
            {
                "key": "diptrace-bom-dashboard",
                "title": _("DipTrace BOM"),
                "description": _("Upload, resolve and check a PCB BOM"),
                "icon": "ti:list-check",
                "source": self.plugin_static_file(
                    "diptrace_bom_dashboard_v024.js:renderDashboardItem"
                ),
                # Three 64 px dashboard rows are required for the title,
                # description and action button without clipping.
                "options": {"width": 3, "height": 3},
                "context": {"url": f"/plugin/{self.SLUG}/"},
            }
        ]

    def get_ui_navigation_items(self, request, context, **kwargs):
        """Add a persistent importer link to the modern InvenTree navigation.

        This is intentionally independent of the dashboard widget: users must
        still be able to reach the importer when a browser cannot load a
        plugin-provided dashboard script.
        """
        return [
            {
                "key": "diptrace-bom-navigation",
                "title": _("DipTrace BOM"),
                "icon": "ti:list-check",
                "options": {"url": f"/plugin/{self.SLUG}/"},
            }
        ]

    def setup_urls(self):
        return [
            path("", self.index, name="index"),
            path("test-connection/", self.test_connection, name="test-connection"),
            path("sync-stock/", self.sync_stock, name="sync-stock"),
            path("preview/", self.preview, name="preview"),
            path("finalize/", self.finalize, name="finalize"),
            path("parts/", self.parts, name="parts"),
        ]


def json_request(request) -> dict:
    if request.method != "POST":
        raise BomImportError("POST required")
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise BomImportError("Invalid JSON request") from exc
