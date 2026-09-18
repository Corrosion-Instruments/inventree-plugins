import io
import unittest

from inventree_diptrace_bom.parser import BomParseError, parse_bom


class ParserTests(unittest.TestCase):
    def test_parses_diptrace_csv_and_ignores_total_row(self):
        source = (
            "#,Designator,Footprint,Comment,Quantity,JLCPCB Part #\n"
            "1,\"C1, C2\",0402,100nF,2,C77014\n"
            "2,R1,0402,10k,1,C25744\n"
            ",,,,3,\n"
        )
        rows = parse_bom(io.BytesIO(source.encode()), "board.csv")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].designators, "C1, C2")
        self.assertEqual(rows[0].quantity, "2")
        self.assertEqual(rows[0].jlcpcb_part, "C77014")

    def test_consolidates_identical_component_rows(self):
        source = (
            "Designator,Footprint,Comment,Quantity,JLCPCB Part #\n"
            "C1,0402,100nF,1,C77014\n"
            "C2,0402,100nF,1,c77014\n"
        )
        rows = parse_bom(io.BytesIO(source.encode()), "board.csv")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].designators, "C1, C2")
        self.assertEqual(rows[0].quantity, "2")

    def test_rejects_missing_quantity_header(self):
        source = "Designator,Comment\nR1,10k\n"
        with self.assertRaisesRegex(BomParseError, "quantity"):
            parse_bom(io.BytesIO(source.encode()), "board.csv")

    def test_parses_xlsx(self):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Designator", "Footprint", "Comment", "Quantity", "JLCPCB Part #"])
        sheet.append(["U1", "QFN-24", "Controller", 1, "C12345"])
        data = io.BytesIO()
        workbook.save(data)
        data.seek(0)

        rows = parse_bom(data, "board.xlsx")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].footprint, "QFN-24")
        self.assertEqual(rows[0].jlcpcb_part, "C12345")


if __name__ == "__main__":
    unittest.main()
