export type ShipType = "cargo" | "tanker" | "passenger" | "fishing" | "pleasure";

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
