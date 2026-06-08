export type ShipType = "cargo" | "tanker" | "passenger" | "fishing" | "pleasure";
export type Sensor = "S1" | "S2";

export interface PortCongestion {
  port_lo_code: string;
  port_name: string;
  port_lat: number;
  port_lon: number;
  hour: string;
  vessels_in_port: number;
  arrivals: number;
  departures: number;
}

export interface PortCall {
  mmsi: number;
  port_lo_code: string;
  port_name: string;
  port_lat: number;
  port_lon: number;
  arrival_ts: string;
  arrival_lat: number;
  arrival_lon: number;
  departure_ts: string | null;
  departure_lat: number | null;
  departure_lon: number | null;
  destination_clean: string;
  detection_method: string;
  arrival_date: string;
}

export interface TimelineState {
  playing: boolean;
  speed: number;
  currentTime: string;
  startTime: string;
  endTime: string;
}

export interface WakePoint {
  lat: number;
  lng: number;
  ts: string;
}

export interface Vessel {
  id: number;
  name: string;
  lat: number;
  lng: number;
  heading: number;
  speed: number;
  shipType: ShipType;
  destination?: string;
  ts?: string;
  imo?: number;
  callSign?: string;
  length?: number;
  width?: number;
  navStatus?: number;
  lastSeenStatic?: string;
}

export interface VesselDetail extends Vessel {
  draught?: number;
  navStatusLabel?: string;
  eta?: string;
}

export interface VesselSummary {
  mmsi: number;
  name: string;
  shipType: ShipType;
}

export interface Bounds {
  west: number;
  east: number;
  south: number;
  north: number;
}

export function shipTypeAISToCategory(code: number | null): ShipType {
  if (code === null) return "pleasure";
  if (code >= 30 && code <= 39) return "fishing";
  if (code >= 60 && code <= 69) return "passenger";
  if (code >= 70 && code <= 79) return "cargo";
  if (code >= 80 && code <= 89) return "tanker";
  return "pleasure";
}

export function shipTypeCodeToLabel(code: number | null): string {
  if (code === null) return "Unknown";
  if (code >= 30 && code <= 39) return "Fishing";
  if (code >= 40 && code <= 49) return "High Speed Craft";
  if (code >= 50 && code <= 59) return "Special Craft";
  if (code >= 60 && code <= 69) return "Passenger";
  if (code >= 70 && code <= 79) return "Cargo";
  if (code >= 80 && code <= 89) return "Tanker";
  return `Other (${code})`;
}

const NAV_STATUS_LABELS: Record<number, string> = {
  0: "Under way using engine",
  1: "At anchor",
  2: "Not under command",
  3: "Restricted manoeuvrability",
  4: "Constrained by draught",
  5: "Moored",
  6: "Aground",
  7: "Engaged in fishing",
  8: "Under way sailing",
  9: "High speed craft",
  10: "Wing in ground",
  11: "Power-driven vessel",
  12: "Push/tow convoy",
  13: "Manoeuvring",
  14: "AIS-SART active",
  15: "Undefined",
};

export function navStatusLabel(code: number | null): string {
  if (code === null) return "Unknown";
  return NAV_STATUS_LABELS[code] ?? `Unknown (${code})`;
}
