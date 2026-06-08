import "maplibre-gl/dist/maplibre-gl.css";
import "./style.css";
import { useRef, useEffect, useState, useCallback } from "react";
import maplibregl from "maplibre-gl";
import { useVessels } from "./useVessels";
import { useTimeline } from "./useTimeline";
import { queryVesselHistory, queryPortCalls } from "./duckdb";
import { useSatellite } from "./useSatellite";
import { useDraw } from "./useDraw";
// import SatelliteControls from "./SatelliteControls"; // réactivé quand satellite revient
import Timeline from "./Timeline";
import Sidebar from "./Sidebar";
import { vesselsToGeoJSON, portsToGeoJSON } from "./mockData";
import type { Bounds, Sensor, ShipType } from "./types";
import { usePorts } from "./usePorts";

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
  useEffect(() => { dateRef.current = date; }, [date]);
  const shiftRef = useRef(false);
  const [bounds, setBounds] = useState<Bounds | null>(null);
  const { vessels, loading, error, ready } = useVessels(date, bounds);
  const { ports } = usePorts(date);

  const [selectedMmsis, setSelectedMmsis] = useState<Set<number>>(new Set());
  const selectedMmsi = selectedMmsis.size === 1 ? [...selectedMmsis][0] : null;

  const timeline = useTimeline(date, bounds, selectedMmsis);
  const displayVessels = timeline.isActive ? timeline.timelineVessels : vessels;
  const [sidebarCollapsed, setSidebarCollapsed] = useState(true);
  const [activeCategories, setActiveCategories] = useState<Set<ShipType>>(
    new Set(["cargo", "tanker", "passenger", "fishing", "pleasure"]),
  );
  const [trajectoryStatus, setTrajectoryStatus] = useState<"loading" | "done" | "error" | "idle">("idle");
  const [trajectoryCount, setTrajectoryCount] = useState(0);
  const [speedRange, setSpeedRange] = useState<[number, number]>([0, 50]);
  const [showLabels, setShowLabels] = useState(false);
  const [legendVisible, setLegendVisible] = useState(true);
  const [_satelliteExpanded, _setSatelliteExpanded] = useState(false);
  void _satelliteExpanded; void _setSatelliteExpanded; // réactivé quand SatelliteControls revient

  const handleSelectVessel = useCallback((mmsi: number, shift: boolean) => {
    if (shift) {
      setSelectedMmsis((prev) => {
        const next = new Set(prev);
        if (next.has(mmsi)) {
          next.delete(mmsi);
        } else {
          next.add(mmsi);
        }
        return next;
      });
    } else {
      setSelectedMmsis(new Set([mmsi]));
    }
    const v = (timeline.isActive ? timeline.timelineVessels : vessels).find((vv) => vv.id === mmsi);
    if (v && mapRef.current) {
      mapRef.current.flyTo({
        center: [v.lng, v.lat],
        zoom: Math.max(mapRef.current.getZoom(), 8),
        duration: 600,
      });
    }
  }, [vessels, timeline.isActive, timeline.timelineVessels]);

  const handleBackToList = useCallback(() => {
    setSelectedMmsis(new Set());
    setTrajectoryStatus("idle");
    setTrajectoryCount(0);
    const map = mapRef.current;
    if (map) {
      const trajSrc = map.getSource("vessel-trajectory") as maplibregl.GeoJSONSource | undefined;
      if (trajSrc) trajSrc.setData({ type: "FeatureCollection", features: [] });
      const radSrc = map.getSource("vessel-radius") as maplibregl.GeoJSONSource | undefined;
      if (radSrc) radSrc.setData({ type: "FeatureCollection", features: [] });
      const wakeSrc = map.getSource("vessel-wake") as maplibregl.GeoJSONSource | undefined;
      if (wakeSrc) wakeSrc.setData({ type: "FeatureCollection", features: [] });
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
  const { mode, drawBounds, startDraw, clear: _clear } = useDraw(mapRef.current);
  const hasDrawArea = drawBounds !== null;
  void _clear; void hasDrawArea; void setSensor; void setScenesOnly; void setSatManualDate; void mode; void startDraw; // réactivé quand SatelliteControls revient
  const satBounds = drawBounds ?? bounds;
  const sat = useSatellite(sensor, satBounds, satDate);

  // Show map when DuckDB is ready, hide splash
  useEffect(() => {
    if (!ready) return;
    setMapVisible(true);
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
  void toggleTheme; // réactivé quand le bouton soleil revient

  // Track shift key state for multi-select
  useEffect(() => {
    const onDown = (e: KeyboardEvent) => { if (e.key === "Shift") shiftRef.current = true; };
    const onUp = (e: KeyboardEvent) => { if (e.key === "Shift") shiftRef.current = false; };
    window.addEventListener("keydown", onDown);
    window.addEventListener("keyup", onUp);
    return () => {
      window.removeEventListener("keydown", onDown);
      window.removeEventListener("keyup", onUp);
    };
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
      "top-left",
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
            "circle-color": "#2563eb",
            "circle-opacity": 0.12,
            "circle-stroke-color": "#2563eb",
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
        m.addImage(arrowId, makeArrowIcon("#2563eb", themeRef.current));
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

      // Wake source + layer for vessel trails
      m.addSource("vessel-wake", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      m.addLayer(
        {
          id: "vessel-wake-layer",
          type: "line",
          source: "vessel-wake",
          paint: {
            "line-color": ["get", "color"],
            "line-width": 2,
            "line-opacity": 0.35,
            "line-blur": 0.5,
          },
        },
        "vessel-point",
      );

      // Port congestion source + layer
      m.addSource("ports", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      m.addLayer(
        {
          id: "ports-congestion",
          type: "circle",
          source: "ports",
          minzoom: 4,
          paint: {
            "circle-radius": [
              "interpolate", ["linear"], ["get", "vessels_in_port"],
              0, 3,
              10, 6,
              50, 10,
              200, 16,
              500, 24,
            ],
            "circle-color": [
              "interpolate", ["linear"], ["get", "congestion"],
              0, "#22c55e",
              0.3, "#eab308",
              0.6, "#f97316",
              1, "#ef4444",
            ],
            "circle-opacity": 0.7,
            "circle-stroke-color": "#fff",
            "circle-stroke-width": 1,
            "circle-stroke-opacity": 0.6,
          },
        },
        "vessel-point",
      );

      // Port congestion hover
      const portHoverPopup = new maplibregl.Popup({
        closeButton: false,
        closeOnClick: false,
        offset: 8,
        className: "hover-tooltip",
      });
      m.on("mousemove", "ports-congestion", (e) => {
        const f = e.features?.[0];
        if (!f?.properties) return;
        m.getCanvas().style.cursor = "pointer";
        const p = f.properties;
        portHoverPopup
          .setLngLat(e.lngLat)
          .setHTML(
            `<span class="hover-tooltip-text">${escapeHtml(p.port_name)} &middot; ${p.vessels_in_port} in port &middot; +${p.arrivals} / -${p.departures}</span>`,
          )
          .addTo(m);
      });
      m.on("mouseleave", "ports-congestion", () => {
        portHoverPopup.remove();
        m.getCanvas().style.cursor = "";
      });

      // Port click: show port_calls popup
      const portDetailPopup = new maplibregl.Popup({
        closeButton: true,
        closeOnClick: false,
        maxWidth: "320px",
        className: "port-detail-popup",
      });
      m.on("click", "ports-congestion", async (e) => {
        const f = e.features?.[0];
        if (!f?.properties) return;
        const p = f.properties;
        const loCode = p.port_lo_code;
        const name = p.port_name;

        portDetailPopup
          .setLngLat(e.lngLat)
          .setHTML(
            `<div class="port-popup-loading">Loading ${escapeHtml(name)}...</div>`,
          )
          .addTo(m);

        try {
          const calls = await queryPortCalls(loCode, dateRef.current, 10);
          if (calls.length === 0) {
            portDetailPopup.setHTML(
              `<div class="port-popup">
                <strong>${escapeHtml(name)}</strong>
                <div class="port-popup-row">No recorded calls today</div>
              </div>`,
            );
            return;
          }
          const rows = calls
            .map(
              (c) =>
                `<div class="port-popup-row">
                  <span>MMSI ${c.mmsi}</span>
                  <span>${c.arrival_ts?.slice(11, 19) ?? "?"}</span>
                  ${c.departure_ts ? `<span>&rarr; ${c.departure_ts.slice(11, 19)}</span>` : '<span class="port-popup-active">in port</span>'}
                </div>`,
            )
            .join("");
          portDetailPopup.setHTML(
            `<div class="port-popup">
              <strong>${escapeHtml(name)}</strong>
              <div class="port-popup-sub">${calls.length} calls (${p.vessels_in_port} in port, +${p.arrivals}/-${p.departures} last hour)</div>
              ${rows}
            </div>`,
          );
        } catch {
          portDetailPopup.setHTML(
            `<div class="port-popup"><strong>${escapeHtml(name)}</strong><div class="port-popup-row">Failed to load calls</div></div>`,
          );
        }
      });

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

        const shift = e.originalEvent?.shiftKey ?? shiftRef.current;

        if (shift) {
          setSelectedMmsis((prev) => {
            const next = new Set(prev);
            if (next.has(mmsi)) next.delete(mmsi);
            else next.add(mmsi);
            return next;
          });
        } else {
          setSelectedMmsis(new Set([mmsi]));
        }

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

      // Click off vessel/port → clear selection
      m.on("click", (e) => {
        if (
          m.queryRenderedFeatures(e.point, {
            layers: ["vessel-point", "ports-congestion"],
          }).length > 0
        )
          return;
        if (e.originalEvent?.shiftKey) return;
        setSelectedMmsis(new Set());
        setTrajectoryStatus("idle");
        setTrajectoryCount(0);
        const radSrc = m.getSource("vessel-radius") as maplibregl.GeoJSONSource | undefined;
        if (radSrc) radSrc.setData({ type: "FeatureCollection", features: [] });
        const trajSrc = m.getSource("vessel-trajectory") as maplibregl.GeoJSONSource | undefined;
        if (trajSrc) trajSrc.setData({ type: "FeatureCollection", features: [] });
        const wakeSrc = m.getSource("vessel-wake") as maplibregl.GeoJSONSource | undefined;
        if (wakeSrc) wakeSrc.setData({ type: "FeatureCollection", features: [] });
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
      map.addImage(arrowId, makeArrowIcon("#2563eb", theme));
      setSourceReady(true);
    });
  }, [theme]);

  // Update vessel data on map (from normal or timeline mode)
  const prevDisplayVesselsRef = useRef(displayVessels);
  useEffect(() => {
    if (!sourceReady || displayVessels === prevDisplayVesselsRef.current) return;
    prevDisplayVesselsRef.current = displayVessels;

    const map = mapRef.current;
    const source = map?.getSource("vessels") as
      | maplibregl.GeoJSONSource
      | undefined;
    if (source) {
      source.setData(vesselsToGeoJSON(displayVessels));
    }
  }, [displayVessels, sourceReady]);

  // Update port congestion data on map
  useEffect(() => {
    console.log(`[Ports layer] sourceReady=${sourceReady}, count=${ports.length}`);
    if (!sourceReady || ports.length === 0) return;
    const map = mapRef.current;
    const source = map?.getSource("ports") as maplibregl.GeoJSONSource | undefined;
    if (source) {
      const geojson = portsToGeoJSON(ports);
      console.log(`[Ports layer] Setting ${geojson.features.length} features on map`);
      source.setData(geojson);
    } else {
      console.warn("[Ports layer] Source 'ports' not found on map");
    }
  }, [ports, sourceReady]);

  // Update wake data on map when timeline is active
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !sourceReady) return;

    const wakeSrc = map.getSource("vessel-wake") as maplibregl.GeoJSONSource | undefined;
    if (!wakeSrc) return;

    if (!timeline.isActive || timeline.wakeData.size === 0) {
      wakeSrc.setData({ type: "FeatureCollection", features: [] });
      return;
    }

    const features: GeoJSON.Feature[] = [];
    const shipTypeMap = new Map<number, string>();
    for (const v of displayVessels) {
      const meta = VESSEL_META.find((m) => m.key === v.shipType);
      shipTypeMap.set(v.id, meta?.color ?? "#888");
    }

    for (const [mmsi, points] of timeline.wakeData) {
      if (points.length < 2) continue;
      if (!selectedMmsis.has(mmsi)) continue;
      const coords: [number, number][] = points.map((p) => [p.lng, p.lat]);
      const color = shipTypeMap.get(mmsi) ?? "#888";
      features.push({
        type: "Feature",
        geometry: { type: "LineString", coordinates: coords },
        properties: { color, mmsi },
      });
    }
    wakeSrc.setData({ type: "FeatureCollection", features });
  }, [timeline.wakeData, timeline.isActive, displayVessels, selectedMmsis, sourceReady]);

  // Update vessel layer filter when categories change
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !sourceReady) return;
    const layer = map.getLayer("vessel-point");
    if (layer) {
      map.setFilter("vessel-point", categoryFilter(activeCategories));
    }
  }, [activeCategories, sourceReady]);

  // Toggle vessel name labels
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !sourceReady) return;
    if (showLabels) {
      if (!map.getLayer("vessel-label")) {
        map.addLayer({
          id: "vessel-label",
          type: "symbol",
          source: "vessels",
          filter: categoryFilter(activeCategories),
          layout: {
            "text-field": ["get", "name"],
            "text-font": ["Open Sans Semibold"],
            "text-size": 11,
            "text-offset": [0, 1.6],
            "text-optional": true,
          },
          paint: {
            "text-color": theme === "dark" ? "#e2e2ed" : "#1a1a2e",
            "text-halo-color": theme === "dark" ? "#0f0f1c" : "#ffffff",
            "text-halo-width": 1.5,
          },
        }, "vessel-point");
      }
    } else {
      if (map.getLayer("vessel-label")) {
        map.removeLayer("vessel-label");
      }
    }
  }, [showLabels, sourceReady, theme]);

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
    <div className={`map-wrap${mapVisible ? " visible" : ""}${!sidebarCollapsed ? " sidebar-open" : ""}`}>
      <Sidebar
        vessels={displayVessels}
        loading={loading || timeline.timelineLoading}
        error={error}
        selectedMmsi={selectedMmsi}
        selectedMmsis={selectedMmsis}
        onSelectVessel={(mmsi) => handleSelectVessel(mmsi, false)}
        onBack={handleBackToList}
        collapsed={sidebarCollapsed}
        onToggleCollapse={() => setSidebarCollapsed((c) => !c)}
        activeCategories={activeCategories}
        onToggleCategory={handleToggleCategory}
        trajectoryStatus={trajectoryStatus}
        trajectoryCount={trajectoryCount}
        speedRange={speedRange}
        onSpeedRangeChange={setSpeedRange}
        showLabels={showLabels}
        onToggleLabels={() => setShowLabels((v) => !v)}
      />
      <div ref={mapContainer} className="map-container" />

      {/* Backdrop — mobile sidebar dismiss */}
      <div
        className={`sidebar-backdrop${!sidebarCollapsed ? " visible" : ""}`}
        onClick={() => setSidebarCollapsed(true)}
      />

      {/* Top bar */}
      <div className="top-bar">

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

      {/* Bottom area — timeline + floating elements */}
      <div className="bottom-bar">
        <Timeline
          currentTime={timeline.currentTime}
          playing={timeline.playing}
          speed={timeline.speed}
          speedOptions={timeline.speedOptions}
          isActive={timeline.isActive}
          loading={timeline.timelineLoading}
          date={timeline.isActive ? timeline.currentTime : date}
          onDateChange={setDate}
          onTogglePlay={timeline.togglePlaying}
          onSpeedChange={timeline.setSpeed}
          onScrub={timeline.setCurrentTime}
          getDayRange={timeline.getDayRange}
        />

        {/* Acquisition time badge */}
        {sensor && sat.acquisitionTime && (
          <div className="acq-badge">
            {sensor === "S1" ? "Sentinel-1" : "Sentinel-2"} ·{" "}
            {sat.acquisitionTime}
          </div>
        )}

        {/* Legend */}
        <div className={`panel panel-lg legend${legendVisible ? " mobile-visible" : ""}`}>
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
            {displayVessels.length.toLocaleString()} vessels
          </div>
        </div>

        {/* Legend toggle (mobile only) */}
        <button
          className={`panel panel-sm legend-toggle${legendVisible ? " active" : ""}`}
          onClick={() => setLegendVisible((v) => !v)}
          title="Toggle legend"
          aria-label="Toggle legend"
        >
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
            <rect x="1" y="1" width="14" height="14" rx="2" stroke="currentColor" strokeWidth="1.5" />
            <line x1="4" y1="5" x2="12" y2="5" stroke="currentColor" strokeWidth="1.5" />
            <line x1="4" y1="8" x2="10" y2="8" stroke="currentColor" strokeWidth="1.5" />
            <line x1="4" y1="11" x2="12" y2="11" stroke="currentColor" strokeWidth="1.5" />
          </svg>
        </button>
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
