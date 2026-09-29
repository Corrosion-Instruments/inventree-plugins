import base64
import hashlib
import hmac
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from inventree_diptrace_bom.jlcpcb import (
    JlcApiError,
    JlcClient,
    JlcCredentials,
    _component_rows,
    availability_summary,
    compact_json,
    make_signature,
    private_stock_quantities,
    public_stock_quantity,
    safe_component_detail,
)


class FakeResponse:
    status_code = 200
    def raise_for_status(self):
        return None

    def json(self):
        return {"code": 200, "message": "success", "data": None}


class FakeSession:
    def __init__(self):
        self.last_kwargs = None

    def post(self, *args, **kwargs):
        self.last_kwargs = kwargs
        return FakeResponse()


class FullPageResponse(FakeResponse):
    def json(self):
        rows = [{"componentCode": f"C{index}"} for index in range(100)]
        return {"code": 200, "message": "success", "data": {"records": rows}}


class FullPageSession(FakeSession):
    def post(self, *args, **kwargs):
        self.last_kwargs = kwargs
        return FullPageResponse()


class JlcTests(unittest.TestCase):
    def test_private_library_uses_jlcpcb_max_page_size(self):
        session = FakeSession()
        client = JlcClient(
            JlcCredentials("app-123", "access-secret", "signing-secret"),
            session=session,
        )
        with patch.dict(sys.modules, {"requests": SimpleNamespace(RequestException=Exception)}):
            result = client.private_library()
        self.assertEqual(result, {})
        self.assertIn('"pageSize":100', session.last_kwargs["data"].decode("utf-8"))
        self.assertNotIn("access-secret", repr(result))
        self.assertNotIn("signing-secret", repr(result))

    def test_incomplete_private_snapshot_is_rejected(self):
        client = JlcClient(
            JlcCredentials("app-123", "access-secret", "signing-secret"),
            session=FullPageSession(),
        )
        with patch.dict(sys.modules, {"requests": SimpleNamespace(RequestException=Exception)}):
            with self.assertRaisesRegex(JlcApiError, "pagination safety limit"):
                client.private_library(max_pages=1)

    def test_signature_uses_documented_canonical_form(self):
        body = compact_json({"componentCodes": ["C77014"]})
        canonical = f"POST\n/example\n123\nabc\n{body}\n"
        expected = base64.b64encode(
            hmac.new(b"secret", canonical.encode(), hashlib.sha256).digest()
        ).decode()
        self.assertEqual(make_signature("POST", "/example", "123", "abc", body, "secret"), expected)

    def test_availability_summarizes_private_buckets(self):
        result = availability_summary(
            {"stockCount": 123},
            {
                "jlcpcbParts": 2,
                "globalSourcingParts": {"quantity": 3},
                "consignedParts": [{"available": 4}],
                "idleStock": 1,
            },
        )
        self.assertEqual(str(result["public_stock"]), "123")
        self.assertEqual(str(result["private_total"]), "6")
        self.assertEqual(str(result["standard_route_stock"]), "125")
        self.assertEqual(str(result["consigned_route_stock"]), "4")
        self.assertEqual(str(result["usable_stock"]), "125")
        self.assertEqual(result["preferred_route"], "standard")
        self.assertNotIn("global_sourcing", result)

    def test_reads_official_detail_response_envelope(self):
        payload = {
            "code": 200,
            "data": {
                "componentDetailResponseVOList": [
                    {"componentCode": "C77014", "stockCount": 10}
                ]
            },
        }
        self.assertEqual(_component_rows(payload)[0]["componentCode"], "C77014")

    def test_private_stock_quantities_ignores_unsupported_buckets(self):
        result = private_stock_quantities(
            {
                "jlcpcbParts": 2,
                "globalSourcingParts": {"quantity": 3},
                "consignedParts": [{"available": 73}],
                "idleStock": 99,
            }
        )
        self.assertEqual(str(result["private"]), "2")
        self.assertEqual(str(result["consigned"]), "73")
        self.assertNotIn("global", result)
        self.assertNotIn("idle", result)

    def test_consigned_route_wins_without_combining_routes(self):
        result = availability_summary(
            {"stockCount": 30},
            {"jlcpcbParts": 20, "consignedParts": 75},
        )
        self.assertEqual(str(result["standard_route_stock"]), "50")
        self.assertEqual(str(result["consigned_route_stock"]), "75")
        self.assertEqual(str(result["usable_stock"]), "75")
        self.assertEqual(result["preferred_route"], "consigned")

    def test_public_stock_quantity_is_strict(self):
        self.assertEqual(str(public_stock_quantity({"stockCount": {"quantity": 17}})), "17")
        with self.assertRaisesRegex(JlcApiError, "invalid stockCount quantity"):
            public_stock_quantity({"stockCount": "not-a-number"})

    def test_invalid_private_quantity_is_rejected_before_stock_changes(self):
        with self.assertRaisesRegex(JlcApiError, "invalid consignedParts quantity"):
            private_stock_quantities({"consignedParts": "not-a-number"})

    def test_safe_component_detail_allow_lists_fields_and_images(self):
        result = safe_component_detail({
            "componentCode": "C77014",
            "componentModel": "GRM155R71H104KE14D",
            "description": "100nF 50V X7R 0402",
            "componentImageUrl": "https://assets.example.com/C77014.jpg",
            "nestedImages": [
                {"largeImage": "https://assets.example.com/C77014-large.jpg"}
            ],
            "secretInternalField": "must-not-leak",
        })
        self.assertEqual(result["componentCode"], "C77014")
        self.assertEqual(len(result["imageUrls"]), 2)
        self.assertNotIn("secretInternalField", result)
        self.assertIn("secretInternalField", result["availableFields"])


if __name__ == "__main__":
    unittest.main()
