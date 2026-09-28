"""Recursive, source-aware build planning for nested InvenTree BOMs."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from .stock_sync import parse_managed_batch


class BuildPlannerError(RuntimeError):
    """A user-facing build planning error."""


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _number(value: Decimal) -> str:
    value = _decimal(value)
    if not value:
        return "0"
    return format(value.normalize(), "f")


@dataclass(frozen=True)
class StockPools:
    """Available stock split into pools which may legally be combined."""

    local: Decimal = Decimal("0")
    public: Decimal = Decimal("0")
    private: Decimal = Decimal("0")
    consigned: Decimal = Decimal("0")
    ignored_global: Decimal = Decimal("0")

    @property
    def standard(self) -> Decimal:
        return self.public + self.private

    @property
    def jlc_usable(self) -> Decimal:
        return max(self.standard, self.consigned)

    @property
    def preferred_jlc_route(self) -> str:
        return "consigned" if self.consigned > self.standard else "public_private"


@dataclass(frozen=True)
class PlanBomLine:
    child_id: int
    quantity: Decimal
    setup_quantity: Decimal = Decimal("0")
    attrition: Decimal = Decimal("0")
    reference: str = ""

    def required(self, parent_quantity: Decimal) -> Decimal:
        if parent_quantity <= 0:
            return Decimal("0")
        multiplier = Decimal("1") + max(self.attrition, Decimal("0")) / Decimal("100")
        return max(self.setup_quantity, Decimal("0")) + parent_quantity * self.quantity * multiplier


@dataclass
class PlanPart:
    pk: int
    name: str
    category: str = ""
    ipn: str = ""
    assembly: bool = False
    stock: StockPools = field(default_factory=StockPools)
    bom: list[PlanBomLine] = field(default_factory=list)

    @property
    def web_url(self) -> str:
        return f"/web/part/{self.pk}/"


class RecursiveBuildPlanner:
    """Evaluate a nested BOM without mixing incompatible JLCPCB stock pools."""

    MAX_BUILD = 1_000_000_000

    def __init__(self, parts: dict[int, PlanPart], root_id: int, route_overrides=None):
        self.parts = parts
        self.root_id = int(root_id)
        if self.root_id not in parts:
            raise BuildPlannerError("The selected assembly was not found")
        self.route_overrides = {
            int(key): value
            for key, value in (route_overrides or {}).items()
            if str(value) in {"local", "jlcpcb"}
        }
        self.order = self._topological_order()

    def route(self, part: PlanPart) -> tuple[str, str]:
        if part.pk in self.route_overrides:
            route = self.route_overrides[part.pk]
            return route, "Manual override"
        category_tokens = {
            token for token in re.split(r"[^a-z0-9]+", part.category.casefold()) if token
        }
        if category_tokens.intersection({"pcb", "pcbs"}) or "printed circuit" in part.category.casefold():
            return "jlcpcb", "PCB category"
        if part.pk == self.root_id:
            return "local", "Selected non-PCB finished product"
        return "local", "Non-PCB assembly"

    def _topological_order(self) -> list[int]:
        order: list[int] = []
        state: dict[int, int] = {}

        def visit(part_id: int, trail: list[int]):
            status = state.get(part_id, 0)
            if status == 2:
                return
            if status == 1:
                names = [self.parts[value].name for value in [*trail, part_id] if value in self.parts]
                raise BuildPlannerError("Circular BOM detected: " + " → ".join(names))
            state[part_id] = 1
            part = self.parts.get(part_id)
            if not part:
                raise BuildPlannerError(f"BOM references missing Part {part_id}")
            for line in part.bom:
                visit(line.child_id, [*trail, part_id])
            state[part_id] = 2
            order.append(part_id)

        visit(self.root_id, [])
        order.reverse()
        return order

    def evaluate(self, quantity: int | Decimal) -> dict[str, Any]:
        units = _decimal(quantity)
        if units < 0:
            raise BuildPlannerError("Build quantity cannot be negative")

        demands: defaultdict[tuple[int, str], Decimal] = defaultdict(Decimal)
        assembly_rows: list[dict[str, Any]] = []
        leaf_rows: list[dict[str, Any]] = []
        build_quantities: dict[int, Decimal] = {self.root_id: units}

        for part_id in self.order:
            part = self.parts[part_id]
            local_demand = demands[(part_id, "local")]
            jlc_demand = demands[(part_id, "jlcpcb")]

            if part_id == self.root_id:
                to_build = units
                local_stock_used = Decimal("0")
            elif part.bom:
                local_stock_used = min(local_demand, part.stock.local)
                to_build = local_demand - local_stock_used + jlc_demand
                build_quantities[part_id] = to_build
            else:
                for context, demand in (("local", local_demand), ("jlcpcb", jlc_demand)):
                    if demand <= 0:
                        continue
                    leaf_rows.append(self._leaf_row(part, context, demand))
                continue

            route, reason = self.route(part)
            assembly_rows.append({
                "part": self._part_json(part),
                "route": route,
                "route_reason": reason,
                "required": _number(units if part_id == self.root_id else local_demand + jlc_demand),
                "finished_stock_used": _number(local_stock_used),
                "to_build": _number(to_build),
            })
            if to_build <= 0:
                continue
            if not part.bom:
                continue
            for line in part.bom:
                demands[(line.child_id, route)] += line.required(to_build)

        shortages = [row for row in leaf_rows if _decimal(row["shortage"]) > 0]
        return {
            "quantity": int(units),
            "feasible": not shortages,
            "assemblies": assembly_rows,
            "leaves": sorted(
                leaf_rows,
                key=lambda row: (_decimal(row["shortage"]) <= 0, row["part"]["name"].casefold()),
            ),
            "shortages": sorted(
                shortages,
                key=lambda row: (_decimal(row["shortage"]) * Decimal("-1"), row["part"]["name"].casefold()),
            ),
        }

    def _leaf_row(self, part: PlanPart, context: str, demand: Decimal) -> dict[str, Any]:
        if context == "jlcpcb":
            available = part.stock.jlc_usable
            source = (
                "Consigned"
                if part.stock.preferred_jlc_route == "consigned"
                else "Public + Private / Pre-order"
            )
        else:
            available = part.stock.local
            source = "Local stock"
        return {
            "part": self._part_json(part),
            "context": context,
            "source": source,
            "required": _number(demand),
            "available": _number(available),
            "shortage": _number(max(demand - available, Decimal("0"))),
            "pools": {
                "local": _number(part.stock.local),
                "public": _number(part.stock.public),
                "private": _number(part.stock.private),
                "standard": _number(part.stock.standard),
                "consigned": _number(part.stock.consigned),
                "ignored_global": _number(part.stock.ignored_global),
            },
        }

    @staticmethod
    def _part_json(part: PlanPart) -> dict[str, Any]:
        return {
            "pk": part.pk,
            "name": part.name,
            "ipn": part.ipn,
            "category": part.category,
            "web_url": part.web_url,
        }

    def maximum_build(self) -> tuple[int, bool]:
        """Return the maximum feasible whole-number build and whether it hit the safety cap."""
        if not self.evaluate(1)["feasible"]:
            return 0, False
        low, high = 1, 2
        while high < self.MAX_BUILD and self.evaluate(high)["feasible"]:
            low = high
            high = min(high * 2, self.MAX_BUILD)
        if high == self.MAX_BUILD and self.evaluate(high)["feasible"]:
            return high, True
        while low + 1 < high:
            middle = (low + high) // 2
            if self.evaluate(middle)["feasible"]:
                low = middle
            else:
                high = middle
        return low, False

    def plan(self, target: int | None = None) -> dict[str, Any]:
        maximum, capped = self.maximum_build()
        check_quantity = target if target is not None else min(maximum + 1, self.MAX_BUILD)
        checked = self.evaluate(check_quantity)
        root = self.parts[self.root_id]
        warnings = []
        if capped:
            warnings.append(
                f"The calculated capacity reached the safety limit of {self.MAX_BUILD:,}."
            )
        if any(part.stock.ignored_global > 0 for part in self.parts.values()):
            warnings.append("Legacy Global Sourcing quantities were found and deliberately ignored.")
        warnings.append("Only exact Part stock is counted; substitute and variant stock is not merged.")
        return {
            "assembly": self._part_json(root),
            "planned_can_build": maximum,
            "target": target,
            "target_feasible": checked["feasible"] if target is not None else None,
            "checked_quantity": check_quantity,
            "routes": checked["assemblies"],
            "requirements": checked["leaves"],
            "bottlenecks": checked["shortages"],
            "warnings": warnings,
        }


class BuildPlannerService:
    """Load an InvenTree BOM and stock snapshot for the pure planner."""

    MAX_PARTS = 2000

    def __init__(self, plugin=None):
        self.plugin = plugin

    def plan(self, part_id: int, *, target=None, route_overrides=None) -> dict[str, Any]:
        try:
            part_id = int(part_id)
        except (TypeError, ValueError):
            raise BuildPlannerError("Select a valid assembly") from None
        if target in (None, ""):
            target_value = None
        else:
            try:
                target_value = int(target)
            except (TypeError, ValueError):
                raise BuildPlannerError("Target quantity must be a whole number") from None
            if target_value <= 0 or target_value > RecursiveBuildPlanner.MAX_BUILD:
                raise BuildPlannerError("Target quantity must be between 1 and 1,000,000,000")
        graph = self._load_graph(part_id)
        if not graph[part_id].bom:
            raise BuildPlannerError("The selected assembly does not have a BOM")
        return RecursiveBuildPlanner(graph, part_id, route_overrides).plan(target_value)

    def _load_graph(self, root_id: int) -> dict[int, PlanPart]:
        from part.models import BomItem, Part

        root = Part.objects.select_related("category").filter(pk=root_id, assembly=True).first()
        if not root:
            raise BuildPlannerError("Select a valid assembly")

        model_parts = {root.pk: root}
        bom_lines: defaultdict[int, list[PlanBomLine]] = defaultdict(list)
        frontier = {root.pk}
        visited: set[int] = set()
        while frontier:
            parent_ids = frontier - visited
            if not parent_ids:
                break
            visited.update(parent_ids)
            items = list(
                BomItem.objects.filter(part_id__in=parent_ids)
                .select_related("sub_part", "sub_part__category")
                .order_by("pk")
            )
            frontier = set()
            for item in items:
                child = item.sub_part
                if not child:
                    continue
                model_parts[child.pk] = child
                frontier.add(child.pk)
                bom_lines[item.part_id].append(PlanBomLine(
                    child_id=child.pk,
                    quantity=_decimal(getattr(item, "quantity", 0)),
                    setup_quantity=_decimal(getattr(item, "setup_quantity", 0)),
                    attrition=_decimal(getattr(item, "attrition", 0)),
                    reference=str(getattr(item, "reference", "") or ""),
                ))
            if len(model_parts) > self.MAX_PARTS:
                raise BuildPlannerError(
                    f"This BOM contains more than {self.MAX_PARTS:,} Parts and cannot be planned safely"
                )

        result: dict[int, PlanPart] = {}
        for part_id, model_part in model_parts.items():
            result[part_id] = PlanPart(
                pk=part_id,
                name=str(model_part.name),
                ipn=str(getattr(model_part, "IPN", "") or ""),
                category=str(getattr(getattr(model_part, "category", None), "pathstring", "") or ""),
                assembly=bool(getattr(model_part, "assembly", False)),
                stock=self._stock_pools(model_part),
                bom=bom_lines[part_id],
            )
        return result

    @staticmethod
    def _stock_pools(part) -> StockPools:
        quantities = defaultdict(Decimal)
        entries = (
            part.stock_entries(in_stock=True, include_variants=False)
            .select_related("location")
            .prefetch_related("allocations", "sales_order_allocations", "transfer_order_allocations")
        )
        for item in entries:
            quantity = max(_decimal(item.unallocated_quantity()), Decimal("0"))
            parsed = parse_managed_batch(getattr(item, "batch", ""))
            if parsed:
                bucket, _code = parsed
                quantities[bucket] += quantity
                continue
            location = getattr(item, "location", None)
            if location is None or not bool(getattr(location, "external", False)):
                quantities["local"] += quantity
        return StockPools(
            local=quantities["local"],
            public=quantities["public"],
            private=quantities["private"],
            consigned=quantities["consigned"],
            ignored_global=quantities["global"],
        )
