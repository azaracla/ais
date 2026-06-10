import type { ShipType } from "../types";

export interface VesselMeta {
  key: ShipType;
  color: string;
  label: string;
}

export const VESSEL_META: VesselMeta[] = [
  { key: "cargo", color: "#3b82f6", label: "Cargo" },
  { key: "tanker", color: "#ef4444", label: "Tanker" },
  { key: "passenger", color: "#22c55e", label: "Passenger" },
  { key: "fishing", color: "#f59e0b", label: "Fishing" },
  { key: "pleasure", color: "#a855f7", label: "Pleasure" },
];

export const ICON_SIZE = 22;
export const ARROW_SIZE = 10;
