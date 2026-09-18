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
        self.assertEqual(str(result["private_total"]), "10")

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

    def test_private_stock_quantities_maps_only_managed_buckets(self):
        result = private_stock_quantities(
            {
                "jlcpcbParts": 2,
                "globalSourcingParts": {"quantity": 3},
                "consignedParts": [{"available": 73}],
                "idleStock": 99,
            }
        )
        self.assertEqual(str(result["private"]), "2")
        self.assertEqual(str(result["global"]), "3")
        self.assertEqual(str(result["consigned"]), "73")
        self.assertNotIn("idle", result)

    def test_invalid_private_quantity_is_rejected_before_stock_changes(self):
        with self.assertRaisesRegex(JlcApiError, "invalid consignedParts quantity"):
            private_stock_quantities({"consignedParts": "not-a-number"})


if __name__ == "__main__":
    unittest.main()
