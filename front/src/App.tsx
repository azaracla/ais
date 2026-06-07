import "maplibre-gl/dist/maplibre-gl.css";
import "./style.css";
import { useRef, useEffect, useState, useCallback } from "react";
import maplibregl from "maplibre-gl";
import { useVessels } from "./useVessels";
import { queryVesselHistory, cancelQuery, getAllVessels } from "./duckdb";
import { useSatellite } from "./useSatellite";
import { useDraw } from "./useDraw";
import SatelliteControls from "./SatelliteControls";
import Sidebar from "./Sidebar";
import { vesselsToGeoJSON } from "./mockData";
import type { Bounds, Sensor, ShipType } from "./types";

const VESSEL_META = [
  { key: "cargo", color: "#3b82f6", label: "Cargo" },
  { key: "tanker", color: "#ef4444", label: "Tanker" },
  { key: "passenger", color: "#22c55e", label: "Passenger" },
  { key: "fishing", color: "#f59e0b", label: "Fishing" },
  { key: "pleasure", color: "#a855f7", label: "Pleasure" },
];

const BASEMAP_LIGHT = {
  version: 8 as const,
  sources: {
    basemap: {
      type: "raster" as const,
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
  layers: [{ id: "basemap", type: "raster" as const, source: "basemap" }],
};

const BASEMAP_DARK = {
  version: 8 as const,
  sources: {
    basemap: {
      type: "raster" as const,
      tiles: [
        "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
        "https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
        "https://c.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
        "https://d.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
      ],
      tileSize: 256,
      attribution:
        '&copy; <a href="https://carto.com/">CARTO</a> &copy; <a href="https://openstreetmap.org">OSM</a>',
    },
  },
  layers: [{ id: "basemap", type: "raster" as const, source: "basemap" }],
};

/* ── Ship-shaped vessel icons ─────────── */

function drawShipIcon(
  color: string,
  size: number,
  theme: "light" | "dark",
): ImageData {
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d")!;
  const cx = size / 2;
  const cy = size / 2;
  const s = size; // use full canvas

  ctx.save();
  ctx.translate(cx, cy);

  // Glow
  const glow = ctx.createRadialGradient(0, 0, s * 0.08, 0, 0, s * 0.55);
  const glowAlpha = theme === "dark" ? 0.35 : 0.18;
  glow.addColorStop(0, color + Math.round(glowAlpha * 255).toString(16).padStart(2, "0"));
  glow.addColorStop(1, "transparent");
  ctx.fillStyle = glow;
  ctx.beginPath();
  ctx.arc(0, 0, s * 0.55, 0, Math.PI * 2);
  ctx.fill();

  // Hull — elongated ellipse pointing up (heading = up on canvas)
  const hullLen = s * 0.4;
  const hullW = s * 0.22;
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.ellipse(0, s * 0.06, hullW, hullLen, 0, 0, Math.PI * 2);
  ctx.fill();

  // Bow point (nose at top)
  ctx.beginPath();
  ctx.moveTo(0, -(s * 0.44));
  ctx.lineTo(-hullW * 0.5, -(hullLen * 0.4));
  ctx.lineTo(hullW * 0.5, -(hullLen * 0.4));
  ctx.closePath();
  ctx.fill();

  // Superstructure (bridge)
  ctx.fillStyle = lightenColor(color, 0.25);
  ctx.beginPath();
  ctx.roundRect(-hullW * 0.45, -hullLen * 0.5, hullW * 0.9, hullLen * 0.55, s * 0.08);
  ctx.fill();

  // Mast (vertical line)
  ctx.strokeStyle = color;
  ctx.lineWidth = Math.max(1, s * 0.06);
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(0, -s * 0.44);
  ctx.lineTo(0, -s * 0.48);
  ctx.stroke();

  // Outline for definition
  ctx.strokeStyle = theme === "dark" ? "rgba(255,255,255,0.2)" : "rgba(0,0,0,0.25)";
  ctx.lineWidth = 0.8;
  ctx.beginPath();
  ctx.ellipse(0, s * 0.06, hullW, hullLen, 0, 0, Math.PI * 2);
  ctx.moveTo(0, -(s * 0.44));
  ctx.lineTo(-hullW * 0.5, -(hullLen * 0.4));
  ctx.lineTo(hullW * 0.5, -(hullLen * 0.4));
  ctx.closePath();
  ctx.stroke();

  ctx.restore();
  return ctx.getImageData(0, 0, size, size);
}

function lightenColor(hex: string, amount: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  const lr = Math.min(255, Math.round(r + (255 - r) * amount));
  const lg = Math.min(255, Math.round(g + (255 - g) * amount));
  const lb = Math.min(255, Math.round(b + (255 - b) * amount));
  return `rgb(${lr},${lg},${lb})`;
}

const ICON_SIZE = 22;
const ARROW_SIZE = 10;

/* ── Bearing between two coordinates ───── */
function bearing(lat1: number, lng1: number, lat2: number, lng2: number): number {
  const φ1 = (lat1 * Math.PI) / 180;
  const φ2 = (lat2 * Math.PI) / 180;
  const Δλ = ((lng2 - lng1) * Math.PI) / 180;
  const y = Math.sin(Δλ) * Math.cos(φ2);
  const x =
    Math.cos(φ1) * Math.sin(φ2) -
    Math.sin(φ1) * Math.cos(φ2) * Math.cos(Δλ);
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}

/* ── Arrow icon for trajectory direction ── */
function makeArrowIcon(color: string, theme: "light" | "dark"): ImageData {
  const s = ARROW_SIZE;
  const canvas = document.createElement("canvas");
  canvas.width = s;
  canvas.height = s;
  const ctx = canvas.getContext("2d")!;
  const cx = s / 2;
  const cy = s / 2;

  // Arrow pointing up (0° = north)
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.moveTo(cx, cy - s * 0.45);          // tip
  ctx.lineTo(cx + s * 0.4, cy + s * 0.45); // bottom-right
  ctx.lineTo(cx + s * 0.12, cy + s * 0.15);
  ctx.lineTo(cx - s * 0.12, cy + s * 0.15);
  ctx.lineTo(cx - s * 0.4, cy + s * 0.45); // bottom-left
  ctx.closePath();
  ctx.fill();

  // Contrasting outline for visibility on both light and dark backgrounds
  ctx.strokeStyle = theme === "dark" ? "rgba(255,255,255,0.9)" : "rgba(0,0,0,0.6)";
  ctx.lineWidth = 1;
  ctx.stroke();

  return ctx.getImageData(0, 0, s, s);
}

function iconImageExpr(): maplibregl.DataDrivenPropertyValueSpecification<string> {
  const cases: (string | maplibregl.Expression)[] = [];
  for (const m of VESSEL_META) {
    cases.push(m.key);
    cases.push(`ship-${m.key}`);
  }
  cases.push("ship-cargo");
  return ["match", ["get", "shipType"], ...cases] as any;
}

function categoryFilter(active: Set<ShipType>): maplibregl.FilterSpecification {
  if (active.size === 5) return ["has", "shipType"];
  return ["in", ["get", "shipType"], ["literal", Array.from(active)]] as any;
}

/* ── Theme helpers ─────────────────────── */

function getInitialTheme(): "light" | "dark" {
  try {
    const stored = localStorage.getItem("ais-theme");
    if (stored === "dark" || stored === "light") return stored;
  } catch {
    /* localStorage unavailable — use default */
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

/* ── App ──────────────────────────────── */

function getYesterdayNoon(): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - 1);
  return `${d.toISOString().slice(0, 10)}T12:00:00.000Z`;
}

const DEFAULT_DATE = getYesterdayNoon();

export default function App() {
  const mapContainer = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const [sourceReady, setSourceReady] = useState(false);
  const sceneAcqTsRef = useRef<number | null>(null);
  const trajectoryGenRef = useRef(0);
  const [mapVisible, setMapVisible] = useState(false);

  const [theme, setTheme] = useState<"light" | "dark">(getInitialTheme);
  const themeRef = useRef(theme);
  useEffect(() => { themeRef.current = theme; }, [theme]);

  const [date, setDate] = useState(DEFAULT_DATE);
  const dateRef = useRef(date);
  dateRef.current = date;
  const [bounds, setBounds] = useState<Bounds | null>(null);
  const { vessels, loading, error, ready } = useVessels(date, bounds);

  const [selectedMmsi, setSelectedMmsi] = useState<number | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [activeCategories, setActiveCategories] = useState<Set<ShipType>>(
    new Set(["cargo", "tanker", "passenger", "fishing", "pleasure"]),
  );
  const [trajectoryStatus, setTrajectoryStatus] = useState<"loading" | "done" | "error" | "idle">("idle");
  const [trajectoryCount, setTrajectoryCount] = useState(0);

  const handleSelectVessel = useCallback((mmsi: number) => {
    setSelectedMmsi(mmsi);
    const v = vessels.find((vv) => vv.id === mmsi);
    if (v && mapRef.current) {
      mapRef.current.flyTo({
        center: [v.lng, v.lat],
        zoom: Math.max(mapRef.current.getZoom(), 8),
        duration: 600,
      });
    }
  }, [vessels]);

  const handleBackToList = useCallback(() => {
    setSelectedMmsi(null);
    setTrajectoryStatus("idle");
    setTrajectoryCount(0);
    const map = mapRef.current;
    if (map) {
      const trajSrc = map.getSource("vessel-trajectory") as maplibregl.GeoJSONSource | undefined;
      if (trajSrc) trajSrc.setData({ type: "FeatureCollection", features: [] });
      const radSrc = map.getSource("vessel-radius") as maplibregl.GeoJSONSource | undefined;
      if (radSrc) radSrc.setData({ type: "FeatureCollection", features: [] });
    }
  }, []);

  const handleToggleCategory = useCallback((cat: ShipType) => {
    setActiveCategories((prev) => {
      const next = new Set(prev);
      if (next.has(cat)) {
        if (next.size > 1) next.delete(cat);
      } else {
        next.add(cat);
      }
      return next;
    });
  }, []);

  const [sensor, setSensor] = useState<Sensor | null>(null);
  const [scenesOnly, setScenesOnly] = useState(true);
  const [satManualDate, setSatManualDate] = useState<string | null>(null);
  const satDate = satManualDate ?? date.slice(0, 10);
  const { mode, drawBounds, startDraw, clear } = useDraw(mapRef.current);
  const hasDrawArea = drawBounds !== null;
  const satBounds = drawBounds ?? bounds;
  const sat = useSatellite(sensor, satBounds, satDate);

  // Show map when DuckDB is ready, hide splash
  useEffect(() => {
    if (!ready) return;
    setMapVisible(true);
    // Preload vessel name cache for search
    getAllVessels().catch((e) => console.warn("Failed to preload vessel cache:", e));
    // Notify splash screen
    const splash = document.getElementById("splash");
    if (splash) {
      splash.classList.add("fade-out");
      setTimeout(() => splash.remove(), 500);
    }
  }, [ready]);

  // Persist theme
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("ais-theme", theme); } catch {}
  }, [theme]);

  const toggleTheme = useCallback(() => {
    setTheme((t) => (t === "light" ? "dark" : "light"));
  }, []);

  // Initialize map
  useEffect(() => {
    if (!mapContainer.current || mapRef.current) return;

    const map = new maplibregl.Map({
      container: mapContainer.current,
      style: theme === "dark" ? BASEMAP_DARK : BASEMAP_LIGHT,
      center: [3.1, 41.7],
      zoom: 4,
      attributionControl: false,
    });

    map.addControl(
      new maplibregl.NavigationControl(),
      "top-right",
    );
    map.addControl(
      new maplibregl.ScaleControl({ unit: "metric", maxWidth: 200 }),
      "bottom-right",
    );

    function initLayers(m: maplibregl.Map) {
      // Register ship icons for current theme
      for (const meta of VESSEL_META) {
        const id = `ship-${meta.key}`;
        if (m.hasImage(id)) m.removeImage(id);
        m.addImage(id, drawShipIcon(meta.color, ICON_SIZE, themeRef.current));
      }

      // Vessel layer with dynamic category filter
      if (!m.getSource("vessels")) {
        m.addSource("vessels", {
          type: "geojson",
          data: { type: "FeatureCollection", features: [] },
        });
      }
      if (m.getLayer("vessel-point")) m.removeLayer("vessel-point");
      m.addLayer({
        id: "vessel-point",
        type: "symbol",
        source: "vessels",
        filter: categoryFilter(activeCategories),
        layout: {
          "icon-image": iconImageExpr(),
          "icon-rotate": ["get", "heading"],
          "icon-rotation-alignment": "map",
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-size": 0.65,
        },
        paint: {
          "icon-opacity": 0.9,
          "icon-opacity-transition": { duration: 300 },
        },
      });

      // Search radius source + layer
      m.addSource("vessel-radius", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      m.addLayer(
        {
          id: "vessel-radius-layer",
          type: "circle",
          source: "vessel-radius",
          paint: {
            "circle-radius": [
              "interpolate",
              ["exponential", 2],
              ["zoom"],
              0,
              ["*", ["get", "searchRadius"], 0.000009],
              10,
              ["*", ["get", "searchRadius"], 0.009],
              20,
              ["*", ["get", "searchRadius"], 9.5],
            ],
            "circle-color": "#6366f1",
            "circle-opacity": 0.12,
            "circle-stroke-color": "#6366f1",
            "circle-stroke-width": 0.5,
            "circle-stroke-opacity": 0.3,
          },
        },
        "vessel-point",
      );

      // Trajectory source + layers
      m.addSource("vessel-trajectory", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      m.addLayer(
        {
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
        },
        "vessel-point",
      );
      // Arrow layer for trajectory direction
      const arrowId = "traj-arrow";
      if (!m.hasImage(arrowId)) {
        m.addImage(arrowId, makeArrowIcon("#6366f1", themeRef.current));
      }
      m.addLayer({
        id: "vt-arrows",
        type: "symbol",
        source: "vessel-trajectory",
        filter: ["all", ["==", ["geometry-type"], "Point"], ["has", "bearing"]],
        layout: {
          "icon-image": arrowId,
          "icon-rotate": ["get", "bearing"],
          "icon-rotation-alignment": "map",
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-size": 0.8,
        },
        paint: {
          "icon-opacity": 0.85,
        },
      }, "vessel-point");

      // Trajectory arrow hover: show time
      const trajPopup = new maplibregl.Popup({
        closeButton: false,
        closeOnClick: false,
        offset: 8,
        className: "traj-tooltip",
      });
      m.on("mousemove", "vt-arrows", (e) => {
        const f = e.features?.[0];
        if (!f?.properties?.ts) return;
        m.getCanvas().style.cursor = "crosshair";
        trajPopup
          .setLngLat(e.lngLat)
          .setHTML(
            `<span class="traj-tooltip-text">${formatTrajTime(f.properties.ts)}</span>`,
          )
          .addTo(m);
      });
      m.on("mouseleave", "vt-arrows", () => {
        trajPopup.remove();
        m.getCanvas().style.cursor = "";
      });

      // Vessel hover: tooltip
      const hoverPopup = new maplibregl.Popup({
        closeButton: false,
        closeOnClick: false,
        offset: 6,
        className: "hover-tooltip",
      });
      m.on("mousemove", "vessel-point", (e) => {
        const f = e.features?.[0];
        if (!f?.properties) return;
        m.getCanvas().style.cursor = "pointer";
        hoverPopup
          .setLngLat(e.lngLat)
          .setHTML(
            `<span class="hover-tooltip-text">${escapeHtml(f.properties.name)} &middot; ${Number(f.properties.speed).toFixed(1)} kn &middot; ${f.properties.heading}&deg;</span>`,
          )
          .addTo(m);
      });
      m.on("mouseleave", "vessel-point", () => {
        hoverPopup.remove();
        m.getCanvas().style.cursor = "";
      });

      // Vessel click: radius + trajectory + sidebar selection
      m.on("click", "vessel-point", async (e) => {
        const f = e.features?.[0];
        if (!f) return;
        const p = f.properties;
        if (!p) return;

        const mmsi = p.id as number | undefined;
        if (!mmsi) return;

        setSelectedMmsi(mmsi);

        // Fly to vessel
        m.flyTo({
          center: (f.geometry as any).coordinates as [number, number],
          zoom: Math.max(m.getZoom(), 8),
          duration: 600,
        });

        // Search radius
        const acqTs = sceneAcqTsRef.current;
        const vesselTs = p.ts ? new Date(p.ts).getTime() : null;
        const radiusSource = m.getSource(
          "vessel-radius",
        ) as maplibregl.GeoJSONSource | undefined;
        if (acqTs && vesselTs && Number(p.speed) > 0 && radiusSource) {
          const timeDiffSec = Math.abs(acqTs - vesselTs) / 1000;
          const radiusMeters = Number(p.speed) * 0.514444 * timeDiffSec;
          radiusSource.setData({
            type: "FeatureCollection",
            features: [
              {
                type: "Feature",
                geometry: f.geometry,
                properties: { searchRadius: radiusMeters },
              },
            ],
          });
        }

        // Trajectory
        const shipType = p.shipType as string | undefined;
        const meta = VESSEL_META.find((mt) => mt.key === shipType);
        const color = meta?.color ?? "#888";
        const arrowId = "traj-arrow";
        if (m.hasImage(arrowId)) m.removeImage(arrowId);
        m.addImage(arrowId, makeArrowIcon(color, themeRef.current));

        const trajSource = m.getSource(
          "vessel-trajectory",
        ) as maplibregl.GeoJSONSource | undefined;

        const gen = ++trajectoryGenRef.current;
        if (p.ts && trajSource) {
          setTrajectoryStatus("loading");
          setTrajectoryCount(0);
          await cancelQuery();
          queryVesselHistory(mmsi, p.ts)
            .then((positions) => {
              if (gen !== trajectoryGenRef.current || !trajSource) return;
              setTrajectoryStatus("done");
              setTrajectoryCount(positions.length);
              if (positions.length < 2) return;
              const coords: [number, number][] = positions.map((pt) => [
                pt.lng,
                pt.lat,
              ]);
              const points: GeoJSON.Feature[] = positions.map((pt, i) => {
                const props: Record<string, unknown> = {
                  color,
                  ts: pt.ts instanceof Date ? pt.ts.toISOString() : String(pt.ts),
                };
                if (pt.heading != null) {
                  props.bearing = pt.heading;
                } else if (i < positions.length - 1) {
                  const next = positions[i + 1];
                  props.bearing = bearing(pt.lat, pt.lng, next.lat, next.lng);
                }
                return {
                  type: "Feature" as const,
                  geometry: {
                    type: "Point" as const,
                    coordinates: [pt.lng, pt.lat] as [number, number],
                  },
                  properties: props,
                };
              });
              const line: GeoJSON.Feature = {
                type: "Feature",
                geometry: { type: "LineString", coordinates: coords },
                properties: { color },
              };
              trajSource.setData({
                type: "FeatureCollection",
                features: [line, ...points],
              });
            })
            .catch(() => {
              if (gen !== trajectoryGenRef.current) return;
              setTrajectoryStatus("error");
            });
        }
      });

      // Click off vessel → clear selection
      m.on("click", (e) => {
        if (
          m.queryRenderedFeatures(e.point, { layers: ["vessel-point"] }).length >
          0
        )
          return;
        setSelectedMmsi(null);
        setTrajectoryStatus("idle");
        setTrajectoryCount(0);
        const radSrc = m.getSource("vessel-radius") as maplibregl.GeoJSONSource | undefined;
        if (radSrc) radSrc.setData({ type: "FeatureCollection", features: [] });
        const trajSrc = m.getSource("vessel-trajectory") as maplibregl.GeoJSONSource | undefined;
        if (trajSrc) trajSrc.setData({ type: "FeatureCollection", features: [] });
      });

      if (!sourceReady) setSourceReady(true);
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
    return () => {
      map.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Switch basemap when theme changes
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;

    const newStyle = theme === "dark" ? BASEMAP_DARK : BASEMAP_LIGHT;
    // Re-register ship icons for new theme after style changes
    map.setStyle(newStyle, { diff: false });
    map.once("style.load", () => {
      for (const meta of VESSEL_META) {
        const id = `ship-${meta.key}`;
        if (map.hasImage(id)) map.removeImage(id);
        map.addImage(id, drawShipIcon(meta.color, ICON_SIZE, theme));
      }
      // Re-create trajectory arrow for current theme
      const arrowId = "traj-arrow";
      if (map.hasImage(arrowId)) map.removeImage(arrowId);
      map.addImage(arrowId, makeArrowIcon("#6366f1", theme));
      setSourceReady(true);
    });
  }, [theme]);

  // Update vessel data on map
  const prevVesselsRef = useRef(vessels);
  useEffect(() => {
    if (!sourceReady || vessels === prevVesselsRef.current) return;
    prevVesselsRef.current = vessels;

    const map = mapRef.current;
    const source = map?.getSource("vessels") as
      | maplibregl.GeoJSONSource
      | undefined;
    if (source) {
      source.setData(vesselsToGeoJSON(vessels));
    }
  }, [vessels, sourceReady]);

  // Update vessel layer filter when categories change
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !sourceReady) return;
    const layer = map.getLayer("vessel-point");
    if (layer) {
      map.setFilter("vessel-point", categoryFilter(activeCategories));
    }
  }, [activeCategories, sourceReady]);

  // Satellite tile layer
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !sourceReady) return;

    if (map.getLayer("satellite-layer")) map.removeLayer("satellite-layer");
    if (map.getSource("satellite")) map.removeSource("satellite");

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

  // Scene acquisition timestamp
  useEffect(() => {
    if (sat.scenes && sat.scenes.features.length > 0) {
      const times = sat.scenes.features
        .map((f) => f.properties?.acquisition_time)
        .filter(Boolean) as string[];
      sceneAcqTsRef.current =
        times.length > 0
          ? new Date(times.sort().pop()!).getTime()
          : null;
    } else if (sensor) {
      sceneAcqTsRef.current = new Date(
        satDate + "T12:00:00Z",
      ).getTime();
    } else {
      sceneAcqTsRef.current = null;
    }
  }, [sat.scenes, sensor, satDate]);

  // Scene footprints
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !sourceReady) return;

    ["scene-fill", "scene-outline"].forEach((id) => {
      if (map.getLayer(id)) map.removeLayer(id);
    });
    if (map.getSource("scenes")) map.removeSource("scenes");

    if (sat.scenes && sat.scenes.features.length > 0) {
      map.addSource("scenes", { type: "geojson", data: sat.scenes });
      map.addLayer(
        {
          id: "scene-fill",
          type: "fill",
          source: "scenes",
          paint: {
            "fill-color": "#fbbf24",
            "fill-opacity": 0.08,
          },
        },
        "vessel-radius-layer",
      );
      map.addLayer(
        {
          id: "scene-outline",
          type: "line",
          source: "scenes",
          paint: {
            "line-color": "#fbbf24",
            "line-width": 1,
            "line-dasharray": [3, 2],
          },
        },
        "vessel-radius-layer",
      );

      const popup = new maplibregl.Popup({
        closeButton: false,
        closeOnClick: false,
        offset: 10,
      });
      map.on("mousemove", "scene-fill", (e) => {
        const f = e.features?.[0];
        if (!f?.properties) return;
        map.getCanvas().style.cursor = "default";
        popup
          .setLngLat(e.lngLat)
          .setHTML(
            `<span style="font:11px system-ui;color:var(--color-text)">${f.properties.acquisition_time}</span>`,
          )
          .addTo(map);
      });
      map.on("mouseleave", "scene-fill", () => {
        popup.remove();
        map.getCanvas().style.cursor = "";
      });
    }
  }, [sat.scenes, sourceReady]);

  return (
    <div className={`map-wrap${mapVisible ? " visible" : ""}`}>
      <Sidebar
        vessels={vessels}
        loading={loading}
        error={error}
        selectedMmsi={selectedMmsi}
        onSelectVessel={handleSelectVessel}
        onBack={handleBackToList}
        collapsed={sidebarCollapsed}
        onToggleCollapse={() => setSidebarCollapsed((c) => !c)}
        activeCategories={activeCategories}
        onToggleCategory={handleToggleCategory}
        trajectoryStatus={trajectoryStatus}
        trajectoryCount={trajectoryCount}
      />
      <div ref={mapContainer} className="map-container" />

      {/* Theme toggle */}
      <button
        className="panel panel-sm theme-toggle"
        onClick={toggleTheme}
        title={theme === "dark" ? "Light mode" : "Dark mode"}
        aria-label="Toggle theme"
      >
        {theme === "dark" ? "☀️" : "🌙"}
      </button>

      {/* Top bar */}
      <div className="top-bar">
        <div className="panel panel-md control-group">
          <label className="control-label">Date (UTC)</label>
          <input
            type="datetime-local"
            className="input-text"
            value={date.slice(0, 16)}
            onChange={(e) =>
              setDate(new Date(e.target.value + "Z").toISOString())
            }
          />
        </div>

        <div className="panel panel-md control-group">
          <span className="control-label">Draw</span>
          <button
            className={`btn${mode === "drawing" ? " btn-active" : ""}`}
            onClick={startDraw}
            disabled={mode === "drawing"}
          >
            {mode === "drawing" ? "Click 2 corners" : "Rectangle"}
          </button>
          <button
            className="btn"
            onClick={clear}
            disabled={!drawBounds && mode !== "drawing"}
          >
            Clear
          </button>
        </div>

        <SatelliteControls
          active={sensor}
          onSensorChange={(s) => {
            setSensor(s);
            setSatManualDate(null);
          }}
          date={satManualDate}
          onDateChange={setSatManualDate}
          sat={sat}
          hasDrawArea={hasDrawArea}
          scenesOnly={scenesOnly}
          onScenesOnlyChange={setScenesOnly}
        />

        {!ready && (
          <div className="panel panel-md badge badge-info">
            <span className="spinner-sm" />
            Initializing DuckDB...
          </div>
        )}

        {loading && (
          <div className="panel panel-md badge badge-loading">
            <span className="spinner-sm" />
            Loading...
          </div>
        )}

        {error && (
          <div className="panel panel-md badge badge-error">{error}</div>
        )}
      </div>

      {/* Acquisition time badge */}
      {sensor && sat.acquisitionTime && (
        <div className="acq-badge">
          {sensor === "S1" ? "Sentinel-1" : "Sentinel-2"} ·{" "}
          {sat.acquisitionTime}
        </div>
      )}

      {/* Legend */}
      <div className="panel panel-lg legend">
        <div className="legend-title">Vessel Types</div>
        {VESSEL_META.map((m) => (
          <div key={m.key} className="legend-item">
            <span
              className="legend-swatch"
              style={{ "--swatch-color": m.color } as React.CSSProperties}
            />
            {m.label}
          </div>
        ))}
        <div className="legend-count">
          {vessels.length.toLocaleString()} vessels
        </div>
      </div>
    </div>
  );
}

function formatTrajTime(iso: string): string {
  const d = new Date(iso);
  const date = d.toISOString().slice(0, 10);
  const time = d.toISOString().slice(11, 19);
  return `${date} ${time} UTC`;
}

function escapeHtml(s: string | undefined | null): string {
  if (!s) return "";
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
