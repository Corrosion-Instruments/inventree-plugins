import unittest

from inventree_diptrace_bom.stock_sync import (
    COMPONENT_CODE_PATTERN,
    managed_batch,
    normalize_code,
    parse_managed_batch,
)


class StockSyncTests(unittest.TestCase):
    def test_managed_batch_round_trip(self):
        marker = managed_batch("consigned", " c9900053998 ")
        self.assertEqual(marker, "DIPTRACE-JLC:consigned:C9900053998")
        self.assertEqual(parse_managed_batch(marker), ("consigned", "C9900053998"))

    def test_normal_stock_batch_is_not_managed(self):
        self.assertIsNone(parse_managed_batch("customer-batch-123"))
        self.assertIsNone(parse_managed_batch("DIPTRACE-JLC:unknown:C123"))

    def test_supported_public_batch_round_trip(self):
        marker = managed_batch("public", "c77014")
        self.assertEqual(parse_managed_batch(marker), ("public", "C77014"))

    def test_legacy_global_batch_remains_parseable_for_retirement(self):
        self.assertEqual(
            parse_managed_batch("DIPTRACE-JLC:global:C123"),
            ("global", "C123"),
        )

    def test_supplier_codes_are_normalized_for_exact_matching(self):
        self.assertEqual(normalize_code(" c77014 "), "C77014")

    def test_only_exact_c_codes_are_public_catalogue_identifiers(self):
        self.assertIsNotNone(COMPONENT_CODE_PATTERN.fullmatch("C77014"))
        self.assertIsNone(COMPONENT_CODE_PATTERN.fullmatch("LCSC-C77014"))


if __name__ == "__main__":
    unittest.main()
