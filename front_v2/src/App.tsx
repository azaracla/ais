import "maplibre-gl/dist/maplibre-gl.css";
import { useRef, useEffect, useState } from "react";
import maplibregl from "maplibre-gl";
import { useVessels } from "./useVessels";
import { queryVesselHistory, cancelQuery } from "./duckdb";
import { useSatellite } from "./useSatellite";
import { useDraw } from "./useDraw";
import SatelliteControls from "./SatelliteControls";
import { vesselsToGeoJSON } from "./mockData";
import type { Bounds, Sensor } from "./types";

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

const DEFAULT_DATE = "2026-05-29T06:00:00.000Z";

export default function App() {
  const mapContainer = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const [sourceReady, setSourceReady] = useState(false);
  const sceneAcqTsRef = useRef<number | null>(null);
  const popupRef = useRef<maplibregl.Popup | null>(null);
  const trajectoryGenRef = useRef(0);

  const [date, setDate] = useState(DEFAULT_DATE);
  const dateRef = useRef(date);
  dateRef.current = date;
  const [bounds, setBounds] = useState<Bounds | null>(null);
  const { vessels, loading, error, ready } = useVessels(date, bounds);

  const [sensor, setSensor] = useState<Sensor | null>(null);
  const [scenesOnly, setScenesOnly] = useState(true);
  const [satManualDate, setSatManualDate] = useState<string | null>(null);
  const satDate = satManualDate ?? date.slice(0, 10);
  const { mode, drawBounds, startDraw, clear } = useDraw(mapRef.current);
  const hasDrawArea = drawBounds !== null;
  const satBounds = drawBounds ?? bounds;
  const sat = useSatellite(sensor, satBounds, satDate);

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
    map.addControl(new maplibregl.ScaleControl({ unit: "metric", maxWidth: 200 }), "bottom-right");

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
      // Close popup + overlays when clicking off a vessel
      m.on("click", (e) => {
        if (m.queryRenderedFeatures(e.point, { layers: ["vessel-point"] }).length > 0) return;
        popupRef.current?.remove();
        popupRef.current = null;
        clearVesselOverlays(m);
      });

      function clearVesselOverlays(map: maplibregl.Map) {
        ["vessel-radius", "vessel-trajectory"].forEach((src) => {
          const s = map.getSource(src) as maplibregl.GeoJSONSource | undefined;
          if (s) s.setData({ type: "FeatureCollection", features: [] });
        });
      }

      // Source + layer for vessel search radius
      m.addSource("vessel-radius", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      m.addLayer({
        id: "vessel-radius-layer",
        type: "circle",
        source: "vessel-radius",
        paint: {
          "circle-radius": [
            "interpolate", ["exponential", 2], ["zoom"],
            0, ["*", ["get", "searchRadius"], 0.000009],
            10, ["*", ["get", "searchRadius"], 0.009],
            20, ["*", ["get", "searchRadius"], 9.5],
          ],
          "circle-color": "#6366f1",
          "circle-opacity": 0.12,
          "circle-stroke-color": "#6366f1",
          "circle-stroke-width": 0.5,
          "circle-stroke-opacity": 0.3,
        },
      }, "vessel-point");

      // Trajectory source + layers (line + dots)
      m.addSource("vessel-trajectory", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      m.addLayer({
        id: "vt-line",
        type: "line",
        source: "vessel-trajectory",
        filter: ["==", ["geometry-type"], "LineString"],
        paint: {
          "line-color": ["get", "color"],
          "line-width": 2,
          "line-dasharray": [4, 3],
          "line-opacity": 0.7,
        },
      }, "vessel-point");
      m.addLayer({
        id: "vt-points",
        type: "circle",
        source: "vessel-trajectory",
        filter: ["==", ["geometry-type"], "Point"],
        paint: {
          "circle-radius": 5,
          "circle-color": ["get", "color"],
          "circle-opacity": 1,
          "circle-stroke-color": "#fff",
          "circle-stroke-width": 1.5,
        },
      }, "vessel-point");

      m.on("click", "vessel-point", async (e) => {
        const f = e.features?.[0];
        if (!f) return;
        const p = f.properties;
        if (!p) return;

        const mmsi = p.id as number | undefined;
        const shipType = p.shipType as string | undefined;
        const color = VESSEL_META.find((m) => m.key === shipType)?.color ?? "#888";
        const trajSource = m.getSource("vessel-trajectory") as maplibregl.GeoJSONSource | undefined;

        // Close previous popup
        popupRef.current?.remove();
        popupRef.current = null;

        // Draw search radius around this vessel
        const acqTs = sceneAcqTsRef.current;
        const vesselTs = p.ts ? new Date(p.ts).getTime() : null;
        const radiusSource = m.getSource("vessel-radius") as maplibregl.GeoJSONSource | undefined;
        if (acqTs && vesselTs && Number(p.speed) > 0 && radiusSource) {
          const timeDiffSec = Math.abs(acqTs - vesselTs) / 1000;
          const radiusMeters = Number(p.speed) * 0.514444 * timeDiffSec;
          radiusSource.setData({
            type: "FeatureCollection",
            features: [{
              type: "Feature",
              geometry: f.geometry,
              properties: { searchRadius: radiusMeters },
            }],
          });
        }

        // Fetch trajectory asynchronously
        const gen = ++trajectoryGenRef.current;
        if (mmsi && p.ts && trajSource) {
          await cancelQuery();
          queryVesselHistory(mmsi, p.ts).then((positions) => {
            if (gen !== trajectoryGenRef.current || !trajSource) return;
            if (positions.length < 2) return;
            const coords: [number, number][] = positions.map((pt) => [pt.lng, pt.lat]);
            const points: GeoJSON.Feature[] = positions.map((pt) => ({
              type: "Feature",
              geometry: { type: "Point", coordinates: [pt.lng, pt.lat] },
              properties: { color },
            }));
            const line: GeoJSON.Feature = {
              type: "Feature",
              geometry: { type: "LineString", coordinates: coords },
              properties: { color },
            };
            trajSource.setData({
              type: "FeatureCollection",
              features: [line, ...points],
            });
          }).catch(() => {});
        }

        const popup = new maplibregl.Popup({ offset: 10 })
          .setLngLat((f.geometry as any).coordinates)
          .setHTML(`
            <div style="font:13px system-ui;">
              <b style="font-size:15px">${p.name}</b>
              <div style="display:grid;grid-template-columns:auto 1fr;gap:2px 10px;margin-top:6px">
                <span style="color:#666">Type</span><span style="text-transform:capitalize">${p.shipType}</span>
                <span style="color:#666">Speed</span><span>${Number(p.speed).toFixed(1)} kn</span>
                <span style="color:#666">Heading</span><span>${p.heading}°</span>
                ${p.destination ? `<span style="color:#666">To</span><span>${p.destination}</span>` : ""}
                ${p.ts ? `<span style="color:#666">AIS at</span><span>${new Date(p.ts).toISOString().slice(0, 19).replace("T", " ")} UTC</span>` : ""}
              </div>
            </div>`)
          .addTo(m);

        popup.on("close", () => clearVesselOverlays(m));

        popupRef.current = popup;
      });
      m.on("mouseenter", "vessel-point", () => { m.getCanvas().style.cursor = "pointer"; });
      m.on("mouseleave", "vessel-point", () => { m.getCanvas().style.cursor = ""; });
      setSourceReady(true);
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
    if (!sourceReady || vessels === prevVesselsRef.current) return;
    prevVesselsRef.current = vessels;

    const map = mapRef.current;
    const source = map?.getSource("vessels") as maplibregl.GeoJSONSource | undefined;
    if (source) {
      source.setData(vesselsToGeoJSON(vessels));
    }
  }, [vessels]);

  // Satellite tile layer
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !sourceReady) return;

    if (map.getLayer("satellite-layer")) {
      map.removeLayer("satellite-layer");
    }
    if (map.getSource("satellite")) {
      map.removeSource("satellite");
    }

    if (sat.tileUrl && !scenesOnly) {
      map.addSource("satellite", {
        type: "raster",
        tiles: [sat.tileUrl],
        tileSize: 256,
      });
      map.addLayer(
        { id: "satellite-layer", type: "raster", source: "satellite" },
        "vessel-radius-layer",
      );
    }
  }, [sat.tileUrl, sourceReady, scenesOnly]);

  // Keep latest scene acquisition timestamp in a ref for vessel radius.
  // Fallback: use satDate as scene time (midnight) when no scenes loaded.
  useEffect(() => {
    if (sat.scenes && sat.scenes.features.length > 0) {
      const times = sat.scenes.features
        .map((f) => f.properties?.acquisition_time)
        .filter(Boolean) as string[];
      sceneAcqTsRef.current = times.length > 0
        ? new Date(times.sort().pop()!).getTime() : null;
    } else if (sensor) {
      sceneAcqTsRef.current = new Date(satDate + "T12:00:00Z").getTime();
    } else {
      sceneAcqTsRef.current = null;
    }
  }, [sat.scenes, sensor, satDate]);

  // Satellite scene footprints
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !sourceReady) return;

    const layers = ["scene-fill", "scene-outline"];
    layers.forEach((id) => { if (map.getLayer(id)) map.removeLayer(id); });
    if (map.getSource("scenes")) map.removeSource("scenes");

    if (sat.scenes && sat.scenes.features.length > 0) {
      map.addSource("scenes", { type: "geojson", data: sat.scenes });
      map.addLayer({
        id: "scene-fill",
        type: "fill",
        source: "scenes",
        paint: { "fill-color": "#fbbf24", "fill-opacity": 0.08 },
      }, "vessel-radius-layer");
      map.addLayer({
        id: "scene-outline",
        type: "line",
        source: "scenes",
        paint: { "line-color": "#fbbf24", "line-width": 1, "line-dasharray": [3, 2] },
      }, "vessel-radius-layer");

      const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10 });
      map.on("mousemove", "scene-fill", (e) => {
        const f = e.features?.[0];
        if (!f?.properties) return;
        map.getCanvas().style.cursor = "default";
        popup.setLngLat(e.lngLat)
          .setHTML(`<span style="font:11px system-ui;color:#333">${f.properties.acquisition_time}</span>`)
          .addTo(map);
      });
      map.on("mouseleave", "scene-fill", () => {
        popup.remove();
        map.getCanvas().style.cursor = "";
      });
    }
  }, [sat.scenes, sourceReady]);

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
          <label style={{ fontWeight: 600 }}>Date (UTC)</label>
          <input
            type="datetime-local"
            value={date.slice(0, 16)}
            onChange={(e) => setDate(new Date(e.target.value + "Z").toISOString())}
            style={{ font: "13px system-ui", border: "1px solid #ccc", borderRadius: 4, padding: "3px 6px" }}
          />
        </div>

        <div style={{
          background: "rgba(255,255,255,0.95)", padding: "8px 14px",
          borderRadius: 8, boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
          font: "13px system-ui", display: "flex", gap: 6, alignItems: "center",
        }}>
          <span style={{ fontWeight: 600, marginRight: 2 }}>Draw</span>
          <button
            onClick={startDraw}
            disabled={mode === "drawing"}
            style={{
              padding: "4px 10px", borderRadius: 4, border: "1px solid #ccc",
              cursor: mode === "drawing" ? "default" : "pointer", fontSize: 12,
              background: mode === "drawing" ? "#6366f1" : "#fff",
              color: mode === "drawing" ? "#fff" : "#333",
              fontWeight: mode === "drawing" ? 600 : 400,
            }}
          >
            {mode === "drawing" ? "Click 2 corners" : "Rectangle"}
          </button>
          <button
            onClick={clear}
            disabled={!drawBounds && mode !== "drawing"}
            style={{
              padding: "4px 10px", borderRadius: 4, border: "1px solid #ccc",
              cursor: (!drawBounds && mode !== "drawing") ? "default" : "pointer",
              fontSize: 12, background: "#fff", color: "#333",
            }}
          >
            Clear
          </button>
        </div>

        <SatelliteControls
          active={sensor}
          onSensorChange={(s) => { setSensor(s); setSatManualDate(null); }}
          date={satManualDate}
          onDateChange={setSatManualDate}
          sat={sat}
          hasDrawArea={hasDrawArea}
          scenesOnly={scenesOnly}
          onScenesOnlyChange={setScenesOnly}
        />

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

      {/* Acquisition time badge */}
      {sensor && sat.acquisitionTime && (
        <div style={{
          position: "absolute", bottom: 28, left: 12,
          background: "rgba(0,0,0,0.55)", padding: "4px 10px",
          borderRadius: 6, font: "11px system-ui", color: "#fff",
          pointerEvents: "none",
        }}>
          {sensor === "S1" ? "Sentinel-1" : "Sentinel-2"} · {sat.acquisitionTime}
        </div>
      )}

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
