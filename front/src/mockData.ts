import type { ShipType, Vessel, PortCongestion } from "./types";

const portNames = [
  "Rotterdam", "Hamburg", "Antwerp", "Le Havre", "Marseille",
  "Genoa", "Barcelona", "Valencia", "Piraeus", "Constanta",
  "Istanbul", "Odessa", "Novorossiysk", "Trieste", "Naples",
  "Lisbon", "Algeciras", "Gibraltar", "Malta", "Limassol",
  "Beirut", "Haifa", "Port Said", "Alexandria", "Tunis",
  "Algiers", "Casablanca", "Dakar", "Abidjan", "Lagos",
];

const shipNames = [
  "Ever Given", "MSC Zoe", "CMA CGM Marco Polo", "Maersk Mc-Kinney Moller",
  "OOCL Hong Kong", "COSCO Shipping Universe", "Mediterranean Harmony",
  "Atlantic Star", "Pacific Voyager", "Northern Light",
  "Southern Cross", "Eastern Pearl", "Western Wind",
  "Cape of Hope", "Blue Horizon", "Red Diamond",
  "Golden Gate", "Silver Stream", "Iron Lady", "Steel Warrior",
  "Ocean Queen", "Sea King", "Wave Rider", "Tide Runner",
  "Storm Breaker", "Calm Waters", "Deep Blue", "Coral Queen",
  "Marine Spirit", "Harbor Master",
];

const shipTypes: ShipType[] = ["cargo", "tanker", "passenger", "fishing", "pleasure"];

function randomInRange(min: number, max: number): number {
  return Math.random() * (max - min) + min;
}

function clampLat(lat: number): number {
  const min = 30;
  const max = 62;
  // wrap around logic for small perturbations
  if (lat < min) return min + (min - lat);
  if (lat > max) return max - (lat - max);
  return lat;
}

function clampLng(lng: number): number {
  const min = -12;
  const max = 36;
  if (lng < min) return min + (min - lng);
  if (lng > max) return max - (lng - max);
  return lng;
}

const hotZones: { lat: number; lng: number; radius: number; count: number }[] = [
  { lat: 51.2, lng: 3.2, radius: 2, count: 200 },   // Antwerp/Rotterdam
  { lat: 49.4, lng: 0.1, radius: 1.5, count: 150 },  // Le Havre
  { lat: 43.3, lng: 5.3, radius: 1.5, count: 120 },  // Marseille
  { lat: 41.3, lng: 2.2, radius: 1, count: 100 },    // Barcelona
  { lat: 44.4, lng: 8.9, radius: 1, count: 80 },     // Genoa
  { lat: 37.9, lng: 23.7, radius: 1, count: 100 },   // Piraeus
  { lat: 36.1, lng: -5.3, radius: 1, count: 80 },    // Gibraltar
  { lat: 41.0, lng: 28.9, radius: 1.5, count: 120 }, // Istanbul
  { lat: 31.2, lng: 29.9, radius: 1, count: 80 },    // Alexandria
  { lat: 46.5, lng: 30.7, radius: 1, count: 60 },    // Odessa
  { lat: 35.9, lng: 14.5, radius: 0.8, count: 60 },  // Malta
  { lat: 51.9, lng: 4.4, radius: 1.5, count: 150 },  // Rotterdam
  { lat: 53.6, lng: 9.9, radius: 1, count: 60 },     // Hamburg
  { lat: 37.5, lng: -0.9, radius: 1, count: 50 },    // Cartagena
  { lat: 38.7, lng: -9.1, radius: 1, count: 70 },    // Lisbon
  { lat: 34.6, lng: 33.0, radius: 0.8, count: 50 },  // Limassol
  { lat: 33.8, lng: 35.5, radius: 0.5, count: 40 },  // Beirut
  { lat: 32.8, lng: 34.9, count: 50, radius: 0.8 },  // Haifa
];

function generateVessel(id: number, lat: number, lng: number): Vessel {
  return {
    id,
    name: shipNames[id % shipNames.length],
    lat,
    lng,
    heading: randomInRange(0, 360),
    speed: randomInRange(0, 25),
    shipType: shipTypes[id % shipTypes.length],
    destination: portNames[Math.floor(Math.random() * portNames.length)],
  };
}

export function generateMockVessels(totalCount = 2000): Vessel[] {
  const vessels: Vessel[] = [];
  let id = 1;

  for (const zone of hotZones) {
    for (let i = 0; i < zone.count && vessels.length < totalCount; i++) {
      const lat = clampLat(zone.lat + randomInRange(-zone.radius, zone.radius));
      const lng = clampLng(zone.lng + randomInRange(-zone.radius, zone.radius));
      vessels.push(generateVessel(id++, lat, lng));
    }
  }

  // fill remaining with scattered positions across the region
  while (vessels.length < totalCount) {
    const lat = randomInRange(30, 62);
    const lng = randomInRange(-12, 36);
    vessels.push(generateVessel(id++, lat, lng));
  }

  return vessels;
}

export function vesselsToGeoJSON(vessels: Vessel[]): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: vessels.map((v) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [v.lng, v.lat] },
      properties: {
        id: v.id,
        name: v.name,
        heading: v.heading,
        speed: Math.round(v.speed * 10) / 10,
        shipType: v.shipType,
        destination: v.destination,
        ts: v.ts,
      },
    })),
  };
}

export function portsToGeoJSON(ports: PortCongestion[]): GeoJSON.FeatureCollection {
  const maxVessels = Math.max(1, ...ports.map((p) => p.vessels_in_port));
  return {
    type: "FeatureCollection",
    features: ports.map((p) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [p.port_lon, p.port_lat] },
      properties: {
        port_lo_code: p.port_lo_code,
        port_name: p.port_name,
        hour: p.hour,
        vessels_in_port: p.vessels_in_port,
        arrivals: p.arrivals,
        departures: p.departures,
        congestion: p.vessels_in_port / maxVessels,
      },
    })),
  };
}
