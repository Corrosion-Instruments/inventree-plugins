import io
import unittest
from pathlib import Path

from inventree_diptrace_bom.catalogue import (
    CatalogueError,
    JlcPageClient,
    _company_metadata,
    _conflicting_codes,
    _is_jlcpcb_name,
    _validate_manufacturer_choice,
    compare_row,
    parse_jlc_page,
)
from inventree_diptrace_bom.parser import parse_bom


PAGE = """
<html><body><h1>CRHA2512AF100MFKEF</h1><dl>
<div><dt>Manufacturer</dt><dd>Vishay Intertech</dd></div>
<div><dt>MFR.Part #</dt><dd>CRHA2512AF100MFKEF</dd></div>
<div><dt>JLCPCB Part #</dt><dd>C4353654</dd></div>
<div><dt>Package</dt><dd>2512</dd></div>
<div><dt>Description</dt><dd>100MΩ 1W Thick Film Resistor</dd></div>
</dl></body></html>
"""


class FakeResponse:
    url = "https://jlcpcb.com/partdetail/C4353654"
    status_code = 200
    text = PAGE
    content = PAGE.encode()

    def raise_for_status(self):
        return None


class FakeSession:
    def get(self, url, timeout):
        assert url == FakeResponse.url
        assert timeout == 20
        return FakeResponse()


class CatalogueTests(unittest.TestCase):
    def test_public_page_fields_and_exact_identity(self):
        part = JlcPageClient(FakeSession()).fetch("C4353654")
        self.assertEqual(part.mpn, "CRHA2512AF100MFKEF")
        self.assertEqual(part.package, "2512")
        self.assertEqual(part.manufacturer, "Vishay Intertech")
        self.assertIn("100MΩ", part.description)
        self.assertIsNone(compare_row({"footprint": "crha2512af100mfkef"}, part))
        self.assertIn("differs", compare_row({"footprint": "wrong part"}, part))

    def test_wrong_page_identity_is_blocked(self):
        with self.assertRaisesRegex(CatalogueError, "identity"):
            parse_jlc_page(PAGE, "C1546")

    def test_unlabelled_page_is_blocked(self):
        with self.assertRaisesRegex(CatalogueError, "identity"):
            parse_jlc_page("<html>Access denied</html>", "C4353654")

    def test_csv_conflicting_code_mpn_is_detected(self):
        source = (
            "Designator,Footprint,Comment,Quantity,JLCPCB Part #\n"
            "R1,AAA,10K,1,C4353654\n"
            "R2,BBB,10K,1,C4353654\n"
        )
        rows = [row.as_dict() for row in parse_bom(io.BytesIO(source.encode()), "bom.csv")]
        self.assertIn("C4353654", _conflicting_codes(rows))

    def test_company_metadata_is_optional_and_company_specific(self):
        record = {
            "componentBrandEn": "Vishay Intertech",
            "manufacturerDescription": "Electronic component maker",
            "manufacturerWebsite": "https://www.vishay.com/",
            "componentSpecification": "Do not use as company description",
        }
        metadata = _company_metadata(record, "Vishay Intertech")
        self.assertEqual(metadata["manufacturer_description"], "Electronic component maker")
        self.assertEqual(metadata["manufacturer_website"], "https://www.vishay.com/")
        self.assertEqual(_company_metadata(record, "Other Co")["manufacturer_description"], "")
        record["manufacturerWebsite"] = "javascript:alert(1)"
        self.assertEqual(_company_metadata(record, "Vishay Intertech")["manufacturer_website"], "")

    def test_jlc_supplier_alias_is_narrow(self):
        self.assertTrue(_is_jlcpcb_name("JLC PCB"))
        self.assertTrue(_is_jlcpcb_name("JLCPCB"))
        self.assertFalse(_is_jlcpcb_name("JLCPCB Europe"))

    def test_alternate_mpn_requires_explicit_confirmation_and_manufacturer(self):
        with self.assertRaisesRegex(CatalogueError, "Confirm"):
            _validate_manufacturer_choice({"new_name": "Other Maker"})
        with self.assertRaisesRegex(CatalogueError, "Select one"):
            _validate_manufacturer_choice({"confirm": True})
        with self.assertRaisesRegex(CatalogueError, "Select one"):
            _validate_manufacturer_choice({"confirm": True, "existing_id": "3", "new_name": "Other Maker"})
        self.assertEqual(_validate_manufacturer_choice({"confirm": True, "existing_id": "3"}), (3, ""))
        self.assertEqual(_validate_manufacturer_choice({"confirm": True, "new_name": "  Other   Maker "}), (None, "Other Maker"))

    def test_alternate_mpn_rejects_invalid_manufacturer_id(self):
        with self.assertRaisesRegex(CatalogueError, "ID is invalid"):
            _validate_manufacturer_choice({"confirm": True, "existing_id": "not-an-id"})

    def test_hidden_manufacturer_role_checkbox_overrides_flex_styling(self):
        page = Path(__file__).resolve().parents[1] / "inventree_diptrace_bom/templates/inventree_diptrace_bom/catalogue.html"
        self.assertIn(".confirmation[hidden] { display: none; }", page.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
