"""InvenTree plugin entry point for DipTrace BOM import."""

from __future__ import annotations

import json
import re

from django.core import signing
from django.core.exceptions import ValidationError
from django.http import HttpResponseForbidden, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.urls import path
from django.utils.translation import gettext_lazy as _

from plugin import InvenTreePlugin
from plugin.mixins import NavigationMixin, SettingsMixin, UrlsMixin, UserInterfaceMixin

from .parser import BomParseError, parse_bom
from .jlcpcb import JlcApiError, JlcClient
from .services import BomImportError, BomImportService

__all__ = []


class DipTraceBomPlugin(
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
    DESCRIPTION = "Normalize DipTrace BOMs and check InvenTree / JLCPCB availability"
    VERSION = "0.1.5"
    AUTHOR = "Corrosion Instruments"
    MIN_VERSION = "1.5.2"

    NAVIGATION_TAB_NAME = "DipTrace BOM"
    NAVIGATION_TAB_ICON = "fas fa-list-check"
    NAVIGATION = [{"name": "DipTrace BOM", "link": "plugin:diptrace-bom:index"}]

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
            ("private_inventory", lambda: client.private_library(page_size=1, max_pages=1)),
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

    def diagnose_private_inventory(self, request):
        """Query one C-code without exposing credentials or unrestricted API data."""
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=403)
        try:
            data = json_request(request)
        except BomImportError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        component_code = str(data.get("component_code") or "").strip().upper()
        if not re.fullmatch(r"C\d{1,20}", component_code):
            return JsonResponse(
                {"error": "Enter a valid JLCPCB C-code, for example C9900053998"},
                status=400,
            )

        service = BomImportService(self)
        credentials = service.jlc_credentials()
        if not credentials.configured:
            return JsonResponse(
                {"error": "Configure the JLCPCB App ID, Access Key and Secret Key first"},
                status=400,
            )

        client = JlcClient(
            credentials,
            host=str(service.setting("JLC_HOST", "https://open.jlcpcb.com")),
            timeout=int(service.setting("JLC_TIMEOUT", 30)),
        )
        try:
            # Intentionally bypass the normal five-minute cache so this is a fresh diagnostic.
            result = client.diagnose_private_library(component_code)
            result["fresh_request"] = True
            return JsonResponse(result)
        except JlcApiError as exc:
            return JsonResponse({"error": str(exc)}, status=502)

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
                "source": self.plugin_static_file("diptrace_bom_dashboard.js"),
                "options": {"width": 3, "height": 2},
                "context": {"url": f"/plugin/{self.SLUG}/"},
            }
        ]

    def get_ui_navigation_items(self, request, context, **kwargs):
        return []

    def setup_urls(self):
        return [
            path("", self.index, name="index"),
            path("test-connection/", self.test_connection, name="test-connection"),
            path(
                "diagnose-private-inventory/",
                self.diagnose_private_inventory,
                name="diagnose-private-inventory",
            ),
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
