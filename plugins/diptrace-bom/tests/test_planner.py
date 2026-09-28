import unittest
from decimal import Decimal
from pathlib import Path

from inventree_diptrace_bom.planner import (
    BuildPlannerError,
    PlanBomLine,
    PlanPart,
    RecursiveBuildPlanner,
    StockPools,
)


def part(pk, name, *, category="", stock=None, bom=None):
    return PlanPart(
        pk=pk,
        name=name,
        category=category,
        assembly=bool(bom),
        stock=stock or StockPools(),
        bom=bom or [],
    )


class PlannerTests(unittest.TestCase):
    def test_nested_bom_uses_finished_stock_and_aggregates_shared_components(self):
        parts = {
            1: part(1, "Polilog", category="Product", bom=[
                PlanBomLine(2, Decimal("1")),
                PlanBomLine(3, Decimal("1")),
                PlanBomLine(4, Decimal("1")),
            ]),
            2: part(2, "AFE", category="PCBs", stock=StockPools(local=Decimal("2")), bom=[
                PlanBomLine(5, Decimal("2")),
                PlanBomLine(6, Decimal("1")),
            ]),
            3: part(3, "Main PCB", category="PCBs", bom=[PlanBomLine(5, Decimal("3"))]),
            4: part(4, "Enclosure", stock=StockPools(local=Decimal("30"))),
            5: part(5, "Shared resistor", stock=StockPools(
                public=Decimal("100"), private=Decimal("20"), consigned=Decimal("80")
            )),
            6: part(6, "AFE sensor", stock=StockPools(consigned=Decimal("100"))),
        }
        result = RecursiveBuildPlanner(parts, 1).plan()
        self.assertEqual(result["planned_can_build"], 24)
        blocker = next(row for row in result["bottlenecks"] if row["part"]["pk"] == 5)
        self.assertEqual(blocker["required"], "121")
        self.assertEqual(blocker["available"], "120")
        self.assertEqual(blocker["shortage"], "1")
        afe = next(row for row in result["routes"] if row["part"]["pk"] == 2)
        self.assertEqual(afe["route"], "jlcpcb")
        self.assertEqual(afe["finished_stock_used"], "2")

    def test_public_private_and_consigned_are_not_added_together(self):
        parts = {
            1: part(1, "Board", category="PCBs", bom=[PlanBomLine(2, Decimal("1"))]),
            2: part(2, "Component", stock=StockPools(
                public=Decimal("50"), private=Decimal("20"), consigned=Decimal("80")
            )),
        }
        planner = RecursiveBuildPlanner(parts, 1)
        result = planner.evaluate(100)
        self.assertFalse(result["feasible"])
        row = result["shortages"][0]
        self.assertEqual(row["available"], "80")
        self.assertEqual(row["shortage"], "20")
        self.assertEqual(row["source"], "Consigned")

    def test_global_sourcing_is_ignored(self):
        parts = {
            1: part(1, "Board", category="PCBs", bom=[PlanBomLine(2, Decimal("1"))]),
            2: part(2, "Component", stock=StockPools(
                public=Decimal("4"), ignored_global=Decimal("1000")
            )),
        }
        result = RecursiveBuildPlanner(parts, 1).plan(5)
        self.assertEqual(result["planned_can_build"], 4)
        self.assertFalse(result["target_feasible"])
        self.assertTrue(any("Global Sourcing" in warning for warning in result["warnings"]))

    def test_route_override_changes_the_allowed_stock_pool(self):
        parts = {
            1: part(1, "Board", category="PCBs", bom=[PlanBomLine(2, Decimal("1"))]),
            2: part(2, "Component", stock=StockPools(local=Decimal("3"), public=Decimal("100"))),
        }
        default = RecursiveBuildPlanner(parts, 1).plan()
        local = RecursiveBuildPlanner(parts, 1, {1: "local"}).plan()
        self.assertEqual(default["planned_can_build"], 100)
        self.assertEqual(local["planned_can_build"], 3)

    def test_setup_quantity_and_attrition_are_applied(self):
        parts = {
            1: part(1, "Board", category="PCBs", bom=[
                PlanBomLine(2, Decimal("2"), setup_quantity=Decimal("3"), attrition=Decimal("10"))
            ]),
            2: part(2, "Component", stock=StockPools(public=Decimal("25"))),
        }
        planner = RecursiveBuildPlanner(parts, 1)
        self.assertTrue(planner.evaluate(10)["feasible"])
        self.assertFalse(planner.evaluate(11)["feasible"])

    def test_circular_bom_is_rejected(self):
        parts = {
            1: part(1, "A", bom=[PlanBomLine(2, Decimal("1"))]),
            2: part(2, "B", bom=[PlanBomLine(1, Decimal("1"))]),
        }
        with self.assertRaisesRegex(BuildPlannerError, "Circular BOM"):
            RecursiveBuildPlanner(parts, 1)

    def test_planner_page_exposes_routes_and_pool_explanation(self):
        root = Path(__file__).resolve().parents[1] / "inventree_diptrace_bom"
        template = (root / "templates/inventree_diptrace_bom/planner.html").read_text(encoding="utf-8")
        plugin = (root / "plugin.py").read_text(encoding="utf-8")
        routes = (root / "static/diptrace_bom_routes.js").read_text(encoding="utf-8")
        self.assertIn("Nested BOM Build Planner", template)
        self.assertIn("Public + Private / Pre-order", template)
        self.assertIn("route_overrides", template)
        self.assertIn('path("planner/"', plugin)
        self.assertIn("redirectBuildPlanner", routes)


if __name__ == "__main__":
    unittest.main()
