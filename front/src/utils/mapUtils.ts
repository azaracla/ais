import type { ShipType } from "../types";
import type { DataDrivenPropertyValueSpecification, FilterSpecification, Expression } from "maplibre-gl";
import { VESSEL_META } from "../constants/vesselMeta";

/**
 * Create a match expression for vessel icons based on ship type
 */
export function iconImageExpr(): DataDrivenPropertyValueSpecification<string> {
  const cases: (string | Expression)[] = [];
  for (const m of VESSEL_META) {
    cases.push(m.key);
    cases.push(`ship-${m.key}`);
  }
  cases.push("ship-cargo");
  return ["match", ["get", "shipType"], ...cases] as any;
}

/**
 * Create a filter for active vessel categories
 */
export function categoryFilter(active: Set<ShipType>): FilterSpecification {
  if (active.size === 5) return ["has", "shipType"];
  return ["in", ["get", "shipType"], ["literal", Array.from(active)]] as any;
}
