import io
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from inventree_diptrace_bom.catalogue import (
    CatalogueError,
    CatalogueImportService,
    JlcPageClient,
    JlcPart,
    apply_sheet_mpn_overrides,
    _company_metadata,
    _conflicting_codes,
    _is_jlcpcb_name,
    _resolve_sheet_manufacturer,
    _select_apply_rows,
    _validate_manufacturer_choice,
    compare_row,
    consignment_source,
    group_manufacturer_parts,
    parse_jlc_page,
    reviewed_sheet_mpn,
    validate_external_link,
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

    def test_consignment_balance_is_positive_evidence_not_a_zero_balance_type(self):
        self.assertEqual(consignment_source("1236"), "consigned")
        self.assertEqual(consignment_source("1236", "catalogue"), "consigned")
        self.assertEqual(consignment_source("0"), "unknown")
        self.assertEqual(consignment_source("0", "catalogue"), "catalogue")
        self.assertEqual(consignment_source("0", "consigned"), "consigned")
        with self.assertRaisesRegex(CatalogueError, "Invalid JLCPCB source choice"):
            consignment_source("0", "global")

    def test_catalogue_reads_private_library_balances_not_inventree_stock(self):
        class ApiClient:
            def private_library(self):
                return {"C1973675": {"consignedParts": 1236},
                        "C47117621": {"consignedParts": 0}}

        quantities = CatalogueImportService(api_client=ApiClient())._consigned_quantities(
            {"C1973675", "C47117621", "C1546"})
        self.assertEqual(quantities, {"C1973675": "1236", "C47117621": "0", "C1546": "0"})
        with self.assertRaisesRegex(CatalogueError, "credentials are required"):
            CatalogueImportService()._consigned_quantities({"C1546"})

        class FailedApiClient:
            def private_library(self):
                raise RuntimeError("network failed")

        with self.assertRaisesRegex(CatalogueError, "Could not verify"):
            CatalogueImportService(api_client=FailedApiClient())._consigned_quantities({"C1546"})

    def test_api_manufacturer_is_authoritative_and_complete_api_skips_page(self):
        class PageClient:
            def fetch(self, code):
                raise AssertionError("A complete API record must not fetch HTML")

        record = {"componentCode": "C49420596", "componentModel": "FBB04009-M24S1143BKM",
                  "componentBrandEn": "TXGA(特思嘉)", "componentSpecification": "SMD-24P",
                  "description": "Connector"}
        part = CatalogueImportService(client=PageClient())._fetch_product("C49420596", record)
        self.assertEqual(part.manufacturer, "TXGA(特思嘉)")
        self.assertEqual(part.source, "api")

    def test_missing_api_description_is_left_blank_without_page_fetch(self):
        class PageClient:
            def fetch(self, code):
                raise AssertionError("A missing description must not fetch HTML")

        record = {"componentCode": "C49420596", "componentModel": "FBB04009-M24S1143BKM",
                  "componentBrandEn": "TXGA(特思嘉)", "componentSpecification": "SMD-24P"}
        part = CatalogueImportService(client=PageClient())._fetch_product("C49420596", record)
        self.assertEqual(part.manufacturer, "TXGA(特思嘉)")
        self.assertEqual(part.description, "")
        self.assertEqual(part.source, "api")

    def test_page_fills_missing_required_api_fields_without_replacing_api_maker(self):
        class PageClient:
            def fetch(self, code):
                return JlcPart(code, "TXGA", "FBB04009-M24S1143BKM", "SMD-24P SMT ROHS",
                               "SMD-24P", f"https://jlcpcb.com/partdetail/{code}")

        record = {"componentCode": "C49420596", "componentModel": "FBB04009-M24S1143BKM",
                  "componentBrandEn": "TXGA(特思嘉)"}
        service = CatalogueImportService(client=PageClient())
        part = service._fetch_product("C49420596", record)
        self.assertEqual(part.manufacturer, "TXGA(特思嘉)")
        self.assertEqual(part.description, "SMD-24P SMT ROHS")
        self.assertEqual(part.package, "SMD-24P")
        self.assertEqual(part.source, "api+page")
        with self.assertRaisesRegex(CatalogueError, "disagree on the MPN"):
            service._fetch_product("C49420596", {**record, "componentModel": "OTHER"})
        with self.assertRaisesRegex(CatalogueError, "API identity"):
            service._fetch_product("C49420596", {**record, "componentCode": "C1546"})

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

    def test_editable_sheet_mpn_preserves_raw_diptrace_value(self):
        original = {"row": 10, "jlcpcb_part": "C106232",
                    "footprint": "RES_0402 - AF0402FR-07100RL"}
        edited = apply_sheet_mpn_overrides([original], {"10": " AF0402FR-07100RL "})[0]
        self.assertEqual(edited["footprint"], original["footprint"])
        self.assertEqual(edited["sheet_mpn"], "AF0402FR-07100RL")
        self.assertEqual(reviewed_sheet_mpn(edited), "AF0402FR-07100RL")
        self.assertEqual(reviewed_sheet_mpn(original), original["footprint"])
        jlc = JlcPart("C106232", "YAGEO", "RC0402FR-07100RL", "", "0402", "https://jlcpcb.com/partdetail/C106232")
        self.assertIn("AF0402FR-07100RL", compare_row(edited, jlc))
        self.assertNotIn("RES_0402", compare_row(edited, jlc))
        self.assertEqual(original["footprint"], "RES_0402 - AF0402FR-07100RL")

    def test_sheet_mpn_override_validates_row_and_length(self):
        rows = [{"row": 10, "jlcpcb_part": "C106232", "footprint": "RAW"}]
        for overrides in ({"11": "OTHER"}, [], {"10": "X" * 101}, {"10": 42}):
            with self.subTest(overrides=overrides), self.assertRaises(CatalogueError):
                apply_sheet_mpn_overrides(rows, overrides)
        self.assertEqual(apply_sheet_mpn_overrides(rows, {})[0]["sheet_mpn"], "RAW")
        self.assertEqual(apply_sheet_mpn_overrides(rows, {"10": ""})[0]["sheet_mpn"], "")
        long_raw = [{**rows[0], "footprint": "X" * 101}]
        self.assertEqual(len(apply_sheet_mpn_overrides(long_raw, {"10": "X" * 101})[0]["sheet_mpn"]), 101)

    def test_conflicting_codes_use_reviewed_mpns(self):
        rows = [{"row": 2, "jlcpcb_part": "C106232", "footprint": "RES_0402 - RC0402FR-07100RL"},
                {"row": 3, "jlcpcb_part": "C106232", "footprint": "RC0402FR-07100RL"}]
        self.assertIn("C106232", _conflicting_codes(rows))
        edited = apply_sheet_mpn_overrides(rows, {"2": "RC0402FR-07100RL"})
        self.assertNotIn("C106232", _conflicting_codes(edited))

    def test_saved_manufacturer_parts_group_under_one_internal_part(self):
        records = [
            types.SimpleNamespace(part_id=1939, MPN="0402CG101J500NT",
                                  manufacturer=types.SimpleNamespace(name="FH (Guangdong Fenghua Advanced Tech)")),
            types.SimpleNamespace(part_id=1939, MPN="CC0402JRNPO9BN101",
                                  manufacturer=types.SimpleNamespace(name="Yageo")),
        ]
        self.assertEqual(group_manufacturer_parts(records), {1939: [
            {"mpn": "0402CG101J500NT", "manufacturer": "FH (Guangdong Fenghua Advanced Tech)"},
            {"mpn": "CC0402JRNPO9BN101", "manufacturer": "Yageo"},
        ]})

    def test_row_save_selects_one_line_and_rejects_full_file_conflicts(self):
        rows = [
            {"row": 2, "jlcpcb_part": "C1546", "footprint": "AAA"},
            {"row": 3, "jlcpcb_part": "C2167623", "footprint": "BBB"},
        ]
        self.assertEqual(_select_apply_rows(rows, 3), [rows[1]])
        self.assertIs(_select_apply_rows(rows, None), rows)
        with self.assertRaisesRegex(CatalogueError, "missing or ambiguous"):
            _select_apply_rows(rows, 4)
        with self.assertRaisesRegex(CatalogueError, "Invalid selected"):
            _select_apply_rows(rows, "oops")
        rows.append({"row": 4, "jlcpcb_part": "C1546", "footprint": "DIFFERENT"})
        with self.assertRaisesRegex(CatalogueError, "conflicting manufacturer numbers"):
            _select_apply_rows(rows, 2)
        self.assertEqual(_select_apply_rows(rows, 3), [rows[1]])

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

    def test_same_manufacturer_mismatch_can_be_confirmed(self):
        company = types.SimpleNamespace(pk=7, name="YAGEO", is_manufacturer=True)

        class Query:
            def first(self):
                return company

        class Manager:
            def filter(self, **kwargs):
                self_pk = kwargs.get("pk")
                self_name = kwargs.get("name__iexact")
                assert self_pk == 7 or self_name == "YAGEO"
                return Query()

        company_models = types.ModuleType("company.models")
        company_models.Company = types.SimpleNamespace(objects=Manager())
        exceptions = types.ModuleType("django.core.exceptions")
        exceptions.ValidationError = type("ValidationError", (Exception,), {})
        with patch.dict(sys.modules, {"company.models": company_models, "django.core.exceptions": exceptions}):
            choice = {"confirm": True, "existing_id": "7"}
            self.assertIs(_resolve_sheet_manufacturer(choice, "YAGEO"), company)
            company.is_manufacturer = False
            with self.assertRaisesRegex(CatalogueError, "Confirm marking"):
                _resolve_sheet_manufacturer(choice, "YAGEO")
            self.assertIs(_resolve_sheet_manufacturer({**choice, "mark_jlc_manufacturer": True}, "YAGEO"), company)

    def test_hidden_manufacturer_role_checkbox_overrides_flex_styling(self):
        page = Path(__file__).resolve().parents[1] / "inventree_diptrace_bom/templates/inventree_diptrace_bom/catalogue.html"
        self.assertIn(".confirmation[hidden] { display: none; }", page.read_text(encoding="utf-8"))

    def test_importers_share_header_navigation_and_design_tokens(self):
        templates = (Path(__file__).resolve().parents[1]
                     / "inventree_diptrace_bom/templates/inventree_diptrace_bom")
        header = (templates / "_header.html").read_text(encoding="utf-8")
        catalogue = (templates / "catalogue.html").read_text(encoding="utf-8")
        importer = (templates / "import.html").read_text(encoding="utf-8")
        self.assertIn('/plugin/diptrace-bom/"', header)
        self.assertIn('/plugin/diptrace-bom/catalogue/"', header)
        self.assertEqual(header.count('aria-current="page"'), 2)
        include = '{% include "inventree_diptrace_bom/_header.html" %}'
        self.assertIn(include, importer)
        self.assertIn(include, catalogue)
        for token in ("--ink:#182230", "--canvas:#f6f7f9", "--action:#171717"):
            self.assertIn(token, importer)
            self.assertIn(token, catalogue)
        self.assertIn('class="file-control"', catalogue)
        self.assertIn('class="table-wrap"', catalogue)

    def test_optional_sheet_part_link_accepts_only_web_urls(self):
        self.assertEqual(validate_external_link(None), "")
        self.assertEqual(validate_external_link(" https://example.com/part/123 "), "https://example.com/part/123")
        for value in ("javascript:alert(1)", "https://user:secret@example.com/part", "https://example.com/bad path"):
            with self.subTest(value=value), self.assertRaises(CatalogueError):
                validate_external_link(value)


if __name__ == "__main__":
    unittest.main()
