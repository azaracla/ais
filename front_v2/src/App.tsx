import "maplibre-gl/dist/maplibre-gl.css";
import { useRef, useEffect, useState } from "react";
import maplibregl from "maplibre-gl";
import { useVessels } from "./useVessels";
import { vesselsToGeoJSON } from "./mockData";
import type { Bounds } from "./types";

const VESSEL_META = [
  { key: "cargo", color: "#3b82f6", label: "Cargo" },
  { key: "tanker", color: "#ef4444", label: "Tanker" },
  { key: "passenger", color: "#22c55e", label: "Passenger" },
  { key: "fishing", color: "#f59e0b", label: "Fishing" },
  { key: "pleasure", color: "#a855f7", label: "Pleasure" },
];

function addTriangleIcons(map: maplibregl.Map) {
  for (const m of VESSEL_META) {
    const id = `triangle-${m.key}`;
    if (map.hasImage(id)) continue;
    const size = 16;
    const canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext("2d")!;
    ctx.translate(size / 2, size / 2);
    ctx.beginPath();
    ctx.moveTo(0, -size / 2 + 1);
    ctx.lineTo(-size / 2 + 2, size / 2 - 1);
    ctx.lineTo(size / 2 - 2, size / 2 - 1);
    ctx.closePath();
    ctx.fillStyle = m.color;
    ctx.fill();
    ctx.strokeStyle = "rgba(0,0,0,0.35)";
    ctx.lineWidth = 0.8;
    ctx.stroke();
    map.addImage(id, ctx.getImageData(0, 0, size, size));
  }
}

function iconImageExpr(): maplibregl.DataDrivenPropertyValueSpecification<string> {
  const cases: (string | maplibregl.Expression)[] = [];
  for (const m of VESSEL_META) {
    cases.push(m.key);
    cases.push(`triangle-${m.key}`);
  }
  cases.push("triangle-cargo");
  return ["match", ["get", "shipType"], ...cases] as any;
}

const DEFAULT_DATE = "2026-05-29T06:00";

export default function App() {
  const mapContainer = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const sourceReady = useRef(false);

  const [date, setDate] = useState(DEFAULT_DATE);
  const [bounds, setBounds] = useState<Bounds | null>(null);
  const { vessels, loading, error, ready } = useVessels(date, bounds);

  // Initialize map once
  useEffect(() => {
    if (!mapContainer.current || mapRef.current) return;

    const map = new maplibregl.Map({
      container: mapContainer.current,
      style: {
        version: 8,
        sources: {
          basemap: {
            type: "raster",
            tiles: [
              "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
              "https://b.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
              "https://c.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
              "https://d.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
            ],
            tileSize: 256,
            attribution:
              '&copy; <a href="https://carto.com/">CARTO</a> &copy; <a href="https://openstreetmap.org">OSM</a>',
          },
        },
        layers: [{ id: "basemap", type: "raster", source: "basemap" }],
      },
      center: [3.1, 41.7],
      zoom: 4,
      // maxBounds: [[-20, 25], [45, 65]],
      attributionControl: false,
    });

    map.addControl(new maplibregl.NavigationControl(), "top-right");

    function initLayers(m: maplibregl.Map) {
      addTriangleIcons(m);
      if (m.getSource("vessels")) return;
      m.addSource("vessels", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      m.addLayer({
        id: "vessel-point",
        type: "symbol",
        source: "vessels",
        layout: {
          "icon-image": iconImageExpr(),
          "icon-rotate": ["get", "heading"],
          "icon-rotation-alignment": "map",
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-size": 0.55,
        },
      });
      m.on("click", "vessel-point", (e) => {
        const f = e.features?.[0];
        if (!f) return;
        const p = f.properties;
        if (!p) return;
        new maplibregl.Popup({ offset: 10 })
          .setLngLat((f.geometry as any).coordinates)
          .setHTML(`
            <div style="font:13px system-ui;">
              <b style="font-size:15px">${p.name}</b>
              <div style="display:grid;grid-template-columns:auto 1fr;gap:2px 10px;margin-top:6px">
                <span style="color:#666">Type</span><span style="text-transform:capitalize">${p.shipType}</span>
                <span style="color:#666">Speed</span><span>${Number(p.speed).toFixed(1)} kn</span>
                <span style="color:#666">Heading</span><span>${p.heading}°</span>
                ${p.destination ? `<span style="color:#666">To</span><span>${p.destination}</span>` : ""}
                ${p.ts ? `<span style="color:#666">AIS at</span><span>${new Date(p.ts).toLocaleString()}</span>` : ""}
              </div>
            </div>`)
          .addTo(m);
      });
      m.on("mouseenter", "vessel-point", () => { m.getCanvas().style.cursor = "pointer"; });
      m.on("mouseleave", "vessel-point", () => { m.getCanvas().style.cursor = ""; });
      sourceReady.current = true;
    }

    map.on("load", () => {
      updateBounds();
      requestAnimationFrame(() => initLayers(map));
    });

    const updateBounds = () => {
      const b = map.getBounds();
      setBounds({
        west: b.getWest(),
        east: b.getEast(),
        south: b.getSouth(),
        north: b.getNorth(),
      });
    };

    map.on("moveend", updateBounds);
    map.on("zoomend", updateBounds);

    mapRef.current = map;
    return () => { map.remove(); mapRef.current = null; };
  }, []);

  // Update vessel data on the map when they change
  const prevVesselsRef = useRef(vessels);
  useEffect(() => {
    if (!sourceReady.current || vessels === prevVesselsRef.current) return;
    prevVesselsRef.current = vessels;

    const source = mapRef.current?.getSource("vessels") as maplibregl.GeoJSONSource | undefined;
    if (source) {
      source.setData(vesselsToGeoJSON(vessels));
    }
  }, [vessels]);

  return (
    <div style={{ position: "relative", width: "100vw", height: "100vh" }}>
      <div ref={mapContainer} style={{ width: "100%", height: "100%" }} />

      {/* Top bar */}
      <div style={{
        position: "absolute", top: 12, left: 12, right: 12,
        display: "flex", gap: 12, alignItems: "center",
      }}>
        <div style={{
          background: "rgba(255,255,255,0.95)", padding: "8px 14px",
          borderRadius: 8, boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
          font: "13px system-ui", display: "flex", gap: 10, alignItems: "center",
        }}>
          <label style={{ fontWeight: 600 }}>Date</label>
          <input
            type="datetime-local"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            style={{ font: "13px system-ui", border: "1px solid #ccc", borderRadius: 4, padding: "3px 6px" }}
          />
        </div>

        {!ready && (
          <div style={{
            background: "rgba(255,255,255,0.95)", padding: "8px 14px",
            borderRadius: 8, boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
            font: "13px system-ui", color: "#888",
          }}>
            Initializing DuckDB...
          </div>
        )}

        {loading && (
          <div style={{
            background: "rgba(255,255,255,0.95)", padding: "8px 14px",
            borderRadius: 8, boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
            font: "13px system-ui", color: "#6366f1",
          }}>
            Loading...
          </div>
        )}

        {error && (
          <div style={{
            background: "rgba(255,255,255,0.95)", padding: "8px 14px",
            borderRadius: 8, boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
            font: "13px system-ui", color: "#ef4444",
          }}>
            {error}
          </div>
        )}
      </div>

      {/* Legend */}
      <div style={{
        position: "absolute", bottom: 28, right: 12,
        background: "rgba(255,255,255,0.95)", padding: "10px 14px",
        borderRadius: 8, boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
        font: "12px system-ui", lineHeight: "20px",
      }}>
        <div style={{ fontWeight: 700, marginBottom: 4, fontSize: 13 }}>Vessel Types</div>
        {VESSEL_META.map((m) => (
          <div key={m.key} style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{
              width: 0, height: 0,
              borderLeft: "5px solid transparent",
              borderRight: "5px solid transparent",
              borderBottom: `8px solid ${m.color}`,
              display: "inline-block",
            }} />
            {m.label}
          </div>
        ))}
        <div style={{ marginTop: 6, color: "#888", fontSize: 11 }}>
          {vessels.length.toLocaleString()} vessels
        </div>
      </div>
    </div>
  );
}
