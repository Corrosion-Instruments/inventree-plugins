import base64
import hashlib
import hmac
import unittest

from inventree_diptrace_bom.jlcpcb import (
    _component_rows,
    availability_summary,
    compact_json,
    make_signature,
    private_inventory_diagnostic,
    response_shape_summary,
)


class JlcTests(unittest.TestCase):
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

    def test_private_inventory_diagnostic_reports_safe_stock_fields(self):
        result = private_inventory_diagnostic(
            "c9900053998",
            {
                "C9900053998": {
                    "componentCode": "C9900053998",
                    "componentModel": "XIAO-nRF52840",
                    "jlcpcbParts": 0,
                    "globalSourcingParts": 0,
                    "consignedParts": 73,
                    "idleStock": 0,
                }
            },
        )
        self.assertTrue(result["found"])
        self.assertEqual(result["inventory"]["consigned"], "73")
        self.assertEqual(result["inventory"]["private_total"], "73")
        self.assertIn("consignedParts", result["returned_field_names"])
        self.assertNotIn("componentModel", result["inventory"])

    def test_private_inventory_diagnostic_reports_missing_code(self):
        result = private_inventory_diagnostic("C123", {"C999": {"componentCode": "C999"}})
        self.assertFalse(result["found"])
        self.assertEqual(result["library_entries_scanned"], 1)
        self.assertEqual(result["returned_field_names"], [])

    def test_response_shape_summary_reports_structure_without_values(self):
        result = response_shape_summary(
            {
                "code": 200,
                "accessKey": "do-not-return",
                "data": {
                    "currentPage": 1,
                    "pageSize": 10,
                    "totalCount": 73,
                    "privateRows": [
                        {
                            "componentCode": "C9900053998",
                            "consignedParts": 73,
                            "secretToken": "do-not-return",
                        }
                    ],
                },
            }
        )
        root = next(item for item in result["response_containers"] if item["path"] == "$")
        row_list = next(
            item
            for item in result["response_containers"]
            if item["path"] == "$.data.privateRows"
        )
        self.assertEqual(root["field_names"], ["code", "data"])
        self.assertEqual(root["redacted_field_count"], 1)
        self.assertEqual(row_list["length"], 1)
        self.assertEqual(row_list["item_field_names"], ["componentCode", "consignedParts"])
        self.assertEqual(result["pagination"]["$.data.totalCount"], "73")
        self.assertNotIn("do-not-return", repr(result))


if __name__ == "__main__":
    unittest.main()
