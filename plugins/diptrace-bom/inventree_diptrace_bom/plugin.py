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
from django.views.decorators.csrf import csrf_exempt

from plugin import InvenTreePlugin
from plugin.mixins import (
    ScheduleMixin,
    SettingsMixin,
    UrlsMixin,
    UserInterfaceMixin,
)

from .catalogue import CatalogueError, CatalogueImportService, apply_sheet_mpn_overrides
from .jlcpcb import JlcApiError, JlcClient, safe_component_detail
from .parser import BomParseError, parse_bom
from .planner import BuildPlannerError, BuildPlannerService
from .services import BomImportError, BomImportService, part_summary
from .stock_sync import JlcStockSyncError, JlcStockSyncService

__all__ = []
logger = logging.getLogger(__name__)


class DipTraceBomPlugin(
    ScheduleMixin,
    SettingsMixin,
    UrlsMixin,
    UserInterfaceMixin,
    InvenTreePlugin,
):
    """Upload DipTrace BOMs, check stock, and finalize InvenTree BOMs."""

    NAME = "DipTraceBomPlugin"
    SLUG = "diptrace-bom"
    TITLE = "DipTrace BOM"
    DESCRIPTION = "Import DipTrace BOMs and synchronize InvenTree / JLCPCB availability"
    VERSION = "0.8.4"
    AUTHOR = "Corrosion Instruments"
    MIN_VERSION = "1.5.2"

    SCHEDULED_TASKS = {
        "jlc-stock-sync": {
            "func": "sync_jlc_stock",
            "schedule": "I",
            "minutes": 5,
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
                "Every 5 minutes, mirror JLCPCB public, pre-order and consigned quantities onto exact existing supplier-SKU matches"
            ),
            "default": False,
            "validator": bool,
        },
        "JLC_PUBLIC_LOCATION": {
            "name": _("JLCPCB Public Catalogue Location"),
            "description": _("External, non-structural location for public stockCount"),
            "model": "stock.stocklocation",
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
                "active_page": "import",
                "show_catalogue_nav": request.user.is_staff,
            },
        )

    def catalogue_index(self, request):
        """Separate catalogue import screen; the BOM editor stays unchanged."""
        if not request.user.is_authenticated or not request.user.is_staff:
            return HttpResponseForbidden("Staff access required")
        return render(request, "inventree_diptrace_bom/catalogue.html", {
            "csrf_token": get_token(request),
            "active_page": "catalogue",
            "show_catalogue_nav": True,
        })

    def planner_index(self, request):
        """Plan nested builds using local stock and valid JLCPCB stock pools."""
        if not request.user.is_authenticated:
            return HttpResponseForbidden("Authentication required")
        return render(request, "inventree_diptrace_bom/planner.html", {
            "title": "Nested BOM Build Planner",
            "csrf_token": get_token(request),
            "initial_part": request.GET.get("part", ""),
            "active_page": "planner",
            "show_catalogue_nav": request.user.is_staff,
        })

    def planner_plan(self, request):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=403)
        try:
            data = json_request(request)
            result = BuildPlannerService(self).plan(
                data.get("part_id"),
                target=data.get("target"),
                route_overrides=data.get("route_overrides") or {},
            )
            return JsonResponse(result)
        except BuildPlannerError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Exception:
            logger.exception("Nested BOM planning failed")
            return JsonResponse({"error": "Build planning failed unexpectedly"}, status=500)

    def _catalogue_service(self):
        credentials = BomImportService(self).jlc_credentials()
        api_client = None
        if credentials.configured:
            api_client = JlcClient(
                credentials,
                host=str(self.get_setting("JLC_HOST")),
                timeout=int(self.get_setting("JLC_TIMEOUT")),
            )
        return CatalogueImportService(
            api_client=api_client,
            supplier_id=BomImportService(self).setting("JLC_SUPPLIER", None),
        )

    def catalogue_preview(self, request):
        if not request.user.is_authenticated or not request.user.is_staff:
            return JsonResponse({"error": "Staff access required"}, status=403)
        if request.method != "POST":
            return JsonResponse({"error": "POST required"}, status=405)
        uploaded = request.FILES.get("file")
        if not uploaded:
            return JsonResponse({"error": "Select a CSV or XLSX BOM file"}, status=400)
        if uploaded.size > 10 * 1024 * 1024:
            return JsonResponse({"error": "BOM file exceeds 10 MB"}, status=400)
        try:
            rows = [row.as_dict() for row in parse_bom(uploaded, uploaded.name)]
            try:
                overrides = json.loads(request.POST.get("mpn_overrides", "{}"))
                source_choices = json.loads(request.POST.get("source_choices", "{}"))
            except json.JSONDecodeError as exc:
                raise CatalogueError("Invalid catalogue preview choices") from exc
            rows = apply_sheet_mpn_overrides(rows, overrides)
            result = self._catalogue_service().preview(rows, source_choices)
            result["preview_token"] = signing.dumps(
                {
                    "rows": rows,
                    "user": request.user.pk,
                    "reviewed_mpns": {
                        str(item["row"]["row"]): item["product"]["mpn"]
                        for item in result["rows"] if item["product"]
                    },
                    "reviewed_manufacturers": {
                        str(item["row"]["row"]): item["product"]["manufacturer"]
                        for item in result["rows"] if item["product"]
                    },
                    "source_choices": source_choices,
                    "reviewed_source_modes": {
                        str(item["row"]["row"]): item["source_mode"]
                        for item in result["rows"]
                    },
                },
                salt="inventree-diptrace-catalogue-preview",
                compress=True,
            )
            return JsonResponse(result)
        except (BomParseError, CatalogueError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Exception:
            logger.exception("Unexpected catalogue preview failure")
            return JsonResponse({"error": "Catalogue preview failed; check the InvenTree server log"}, status=500)

    def catalogue_apply(self, request):
        if not request.user.is_authenticated or not request.user.is_superuser:
            return JsonResponse({"error": "Superuser access required"}, status=403)
        try:
            data = json_request(request)
            preview = signing.loads(
                data.get("preview_token", ""),
                salt="inventree-diptrace-catalogue-preview",
                max_age=3600,
            )
            if preview.get("user") != request.user.pk:
                return JsonResponse({"error": "Preview belongs to another user"}, status=403)
            category_ids = data.get("categories") or {}
            manufacturer_choices = data.get("manufacturers") or {}
            package_choices = data.get("packages") or {}
            sheet_links = data.get("sheet_links") or {}
            reviewed_mpns = preview.get("reviewed_mpns")
            reviewed_manufacturers = preview.get("reviewed_manufacturers")
            source_choices = preview.get("source_choices")
            reviewed_source_modes = preview.get("reviewed_source_modes")
            if (not isinstance(category_ids, dict) or not isinstance(manufacturer_choices, dict)
                    or not isinstance(package_choices, dict)
                    or not isinstance(sheet_links, dict)
                    or not isinstance(reviewed_mpns, dict) or not isinstance(reviewed_manufacturers, dict)
                    or not isinstance(source_choices, dict) or not isinstance(reviewed_source_modes, dict)
                    or not isinstance(preview.get("rows"), list)):
                raise CatalogueError("Invalid catalogue preview data")
            result = self._catalogue_service().apply(
                preview["rows"], category_ids, manufacturer_choices, package_choices, sheet_links, reviewed_mpns,
                reviewed_manufacturers, source_choices, reviewed_source_modes,
                request.user, only_row=data.get("row_number")
            )
            return JsonResponse(result)
        except signing.SignatureExpired:
            return JsonResponse({"error": "Preview expired; upload the BOM again"}, status=400)
        except signing.BadSignature:
            return JsonResponse({"error": "Invalid preview token"}, status=400)
        except (CatalogueError, BomImportError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Exception:
            logger.exception("Unexpected catalogue apply failure")
            return JsonResponse({"error": "Catalogue import failed; no records were saved"}, status=500)

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

    @csrf_exempt
    def component_details_api(self, request):
        """Return sanitized JLC facts using the plugin's protected credentials."""
        user = request.user
        if not user.is_authenticated:
            try:
                from rest_framework.authentication import TokenAuthentication

                authenticated = TokenAuthentication().authenticate(request)
            except Exception:
                authenticated = None
            if authenticated:
                user = authenticated[0]
        if not user.is_authenticated or not user.is_superuser:
            return JsonResponse({"error": "Superuser access required"}, status=403)
        if request.method != "POST":
            return JsonResponse({"error": "POST required"}, status=405)
        try:
            data = json_request(request)
            values = data.get("component_codes")
            if not isinstance(values, list) or not 1 <= len(values) <= 1000:
                return JsonResponse(
                    {"error": "Provide between 1 and 1,000 component_codes"},
                    status=400,
                )
            from .catalogue import CODE_RE

            codes = list(
                dict.fromkeys(str(value or "").strip().upper() for value in values)
            )
            invalid = [code for code in codes if not CODE_RE.fullmatch(code)]
            if invalid:
                return JsonResponse(
                    {"error": "Invalid JLCPCB component code", "invalid": invalid},
                    status=400,
                )
            service = self._catalogue_service()
            if service.api_client is None:
                return JsonResponse(
                    {"error": "JLCPCB API credentials are not configured"},
                    status=400,
                )
            records = service.api_client.component_details(codes)
            safe = {
                code: safe_component_detail(records[code])
                for code in codes
                if code in records
            }
            return JsonResponse(
                {
                    "requested": len(codes),
                    "found": len(safe),
                    "missing": [code for code in codes if code not in safe],
                    "records": safe,
                }
            )
        except JlcApiError as exc:
            return JsonResponse({"error": str(exc)}, status=502)
        except Exception:
            logger.exception("JLCPCB component detail lookup failed")
            return JsonResponse(
                {"error": "Component detail lookup failed unexpectedly"},
                status=500,
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

    def assemblies(self, request):
        """List assembly categories or create a new BOM target assembly."""
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=403)

        from part.models import Part, PartCategory

        if request.method == "GET":
            categories = [
                {
                    "pk": category.pk,
                    "name": category.name,
                    "path": category.pathstring or category.name,
                }
                for category in PartCategory.objects.filter(structural=False).order_by("name")
            ]
            categories.sort(key=lambda category: category["path"].casefold())
            return JsonResponse({"categories": categories})

        try:
            data = json_request(request)
        except BomImportError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        from django.db import IntegrityError, transaction
        from users.permissions import check_user_permission

        if not check_user_permission(request.user, Part, "add"):
            return JsonResponse({"error": "Missing add permission for Part"}, status=403)

        name = str(data.get("name") or "").strip()
        ipn = str(data.get("ipn") or "").strip()
        description = str(data.get("description") or "").strip()
        category_id = data.get("category_id")
        if not name:
            return JsonResponse({"error": "Enter an assembly name"}, status=400)
        if len(name) > 100:
            return JsonResponse({"error": "Assembly name must be 100 characters or fewer"}, status=400)
        if len(ipn) > 100:
            return JsonResponse({"error": "IPN must be 100 characters or fewer"}, status=400)
        if len(description) > 250:
            return JsonResponse({"error": "Description must be 250 characters or fewer"}, status=400)

        try:
            category = PartCategory.objects.filter(pk=int(category_id), structural=False).first()
        except (TypeError, ValueError):
            category = None
        if category is None:
            return JsonResponse({"error": "Choose a non-structural category for the assembly"}, status=400)

        if ipn:
            existing = Part.objects.filter(IPN__iexact=ipn).first()
            if existing:
                if existing.assembly:
                    return JsonResponse({
                        "assembly": part_summary(existing),
                        "created": False,
                    })
                return JsonResponse({
                    "error": f"IPN '{ipn}' already belongs to a Part that is not marked as an assembly"
                }, status=409)

        try:
            with transaction.atomic():
                assembly = Part(
                    name=name,
                    IPN=ipn,
                    description=description,
                    category=category,
                    assembly=True,
                    component=True,
                    purchaseable=False,
                    active=True,
                    creation_user=request.user,
                )
                assembly.full_clean()
                assembly.save()
        except (IntegrityError, ValidationError) as exc:
            return JsonResponse({"error": f"Could not create assembly: {exc}"}, status=400)

        return JsonResponse({
            "assembly": part_summary(assembly),
            "created": True,
        }, status=201)

    def get_ui_dashboard_items(self, request, context, **kwargs):
        return [
            {
                "key": "diptrace-bom-dashboard",
                "title": _("DipTrace BOM Tools"),
                "description": _("Import BOMs, plan nested builds and manage JLC parts"),
                "icon": "ti:list-check",
                "source": self.plugin_static_file(
                    "diptrace_bom_dashboard_v082.js:renderDashboardItem"
                ),
                "options": {"width": 4, "height": 4},
                "context": {
                    "importer_url": f"/plugin/{self.SLUG}/",
                    "planner_url": f"/plugin/{self.SLUG}/planner/",
                    "catalogue_url": (
                        f"/plugin/{self.SLUG}/catalogue/" if request.user.is_staff else ""
                    ),
                },
            }
        ]

    def setup_urls(self):
        return [
            path("", self.index, name="index"),
            path("planner/", self.planner_index, name="planner"),
            path("planner/plan/", self.planner_plan, name="planner-plan"),
            path("catalogue/", self.catalogue_index, name="catalogue"),
            path("catalogue/preview/", self.catalogue_preview, name="catalogue-preview"),
            path("catalogue/apply/", self.catalogue_apply, name="catalogue-apply"),
            path("test-connection/", self.test_connection, name="test-connection"),
            path("api/component-details/", self.component_details_api, name="component-details-api"),
            path("sync-stock/", self.sync_stock, name="sync-stock"),
            path("preview/", self.preview, name="preview"),
            path("finalize/", self.finalize, name="finalize"),
            path("parts/", self.parts, name="parts"),
            path("assemblies/", self.assemblies, name="assemblies"),
        ]


def json_request(request) -> dict:
    if request.method != "POST":
        raise BomImportError("POST required")
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise BomImportError("Invalid JSON request") from exc
