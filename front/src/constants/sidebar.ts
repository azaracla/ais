import type { ShipType } from "../types";

export const SHIP_TYPE_KEYS: ShipType[] = [
  "cargo",
  "tanker",
  "passenger",
  "fishing",
  "pleasure",
];

export const CAT_COLORS: Record<ShipType, string> = {
  cargo: "#3b82f6",
  tanker: "#ef4444",
  passenger: "#22c55e",
  fishing: "#f59e0b",
  pleasure: "#a855f7",
};

export const SORT_OPTIONS = [
  { key: "speed" as const, label: "Speed" },
  { key: "name" as const, label: "Name" },
  { key: "type" as const, label: "Type" },
] as const;

export const SIDEBAR_ITEM_SIZE = 56; // Height of each sidebar item in pixels
