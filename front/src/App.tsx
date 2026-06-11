import "./styles/index.css";
import { useRef, useEffect, useState, useCallback } from "react";
import { createRoot } from "react-dom/client";
import type maplibregl from "maplibre-gl";
import { useVessels } from "./hooks/useVessels";
import { useTimeline } from "./hooks/useTimeline";
import { queryVesselHistory, queryPortCalls } from "./duckdb";
import { useSatellite } from "./hooks/useSatellite";
import { useDraw } from "./hooks/useDraw";
import Sidebar from "./Sidebar";
import VesselPopup from "./VesselPopup";
import BottomBar from "./components/BottomBar";
import TopBar, { StatusBadge, Spinner } from "./components/TopBar";
import { vesselsToGeoJSON, portsToGeoJSON } from "./mockData";
import type { Bounds, Sensor, ShipType, Vessel } from "./types";
import { usePorts } from "./hooks/usePorts";
import { VESSEL_META, ICON_SIZE } from "./constants/vesselMeta";
import { BASEMAP_LIGHT, BASEMAP_DARK } from "./constants/basemaps";
import { drawShipIcon, makeArrowIcon } from "./utils/shipIcons";
import { categoryFilter, iconImageExpr } from "./utils/mapUtils";
import { getInitialTheme } from "./utils/themeUtils";
import { formatTrajTime, escapeHtml } from "./utils/formatUtils";
import { bearing } from "./utils/geoUtils";

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
  const portHoverPopupRef = useRef<maplibregl.Popup | null>(null);
  const portDetailPopupRef = useRef<maplibregl.Popup | null>(null);
  const vesselDetailPopupRef = useRef<maplibregl.Popup | null>(null);
  
  // Lazy load MapLibre GL
  const [maplibreglRuntime, setMaplibreglRuntime] = useState<typeof maplibregl | null>(null);
  const [maplibreLoading, setMaplibreLoading] = useState(true);
  
  useEffect(() => {
    Promise.all([
      import("maplibre-gl"),
      import("maplibre-gl/dist/maplibre-gl.css")
    ]).then(([module]) => {
      setMaplibreglRuntime(module.default);
      setMaplibreLoading(false);
    });
  }, []);

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

  const timeline = useTimeline(date, bounds, selectedMmsis, setDate);
  const displayVessels = timeline.isActive ? timeline.timelineVessels : vessels;
  const [sidebarCollapsed, setSidebarCollapsed] = useState(true);
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    try {
      const saved = localStorage.getItem("ais-sidebar-width");
      return saved ? Math.max(200, Math.min(600, Number(saved))) : 380;
    } catch {
      return 380;
    }
  });
  const [activeCategories, setActiveCategories] = useState<Set<ShipType>>(
    new Set(["cargo", "tanker", "passenger", "fishing", "pleasure"]),
  );
  const [trajectoryStatus, setTrajectoryStatus] = useState<"loading" | "done" | "error" | "idle">("idle");
  const [trajectoryCount, setTrajectoryCount] = useState(0);
  const [speedRange, setSpeedRange] = useState<[number, number]>([0, 50]);
  const [showLabels, setShowLabels] = useState(false);
  const [legendVisible, setLegendVisible] = useState(true);
  const [showPortCongestion, setShowPortCongestion] = useState(false);

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
    vesselDetailPopupRef.current?.remove();
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

  const [sensor, _setSensor] = useState<Sensor | null>(null);
  const [_scenesOnly, _setScenesOnly] = useState(true);
  const [_satManualDate, _setSatManualDate] = useState<string | null>(null);
  const satDate = _satManualDate ?? date.slice(0, 10);
  const { mode: _mode, drawBounds, startDraw: _startDraw, clear: _clear } = useDraw(mapRef.current);
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

  // Persist sidebar width
  useEffect(() => {
    try { localStorage.setItem("ais-sidebar-width", String(sidebarWidth)); } catch {}
  }, [sidebarWidth]);

  const toggleTheme = useCallback(() => {
    setTheme((t) => (t === "light" ? "dark" : "light"));
  }, []);

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
    if (!maplibreglRuntime || !mapContainer.current || mapRef.current) return;
    
    // Extract non-null maplibregl for type safety in closures
    const ml = maplibreglRuntime;

    const map = new ml.Map({
      container: mapContainer.current,
      style: theme === "dark" ? BASEMAP_DARK : BASEMAP_LIGHT,
      center: [3.1, 41.7],
      zoom: 4,
      attributionControl: false,
    });

    map.addControl(
      new ml.NavigationControl(),
      "top-right",
    );
    map.addControl(
      new ml.ScaleControl({ unit: "metric", maxWidth: 200 }),
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

      // Micro-dots layer (zoom 0–7) — colored circles, no heading
      if (m.getLayer("vessel-dots")) m.removeLayer("vessel-dots");
      m.addLayer({
        id: "vessel-dots",
        type: "circle",
        source: "vessels",
        filter: categoryFilter(activeCategories),
        minzoom: 0,
        maxzoom: 8,
        paint: {
          "circle-radius": [
            "interpolate", ["linear"], ["zoom"],
            2, 1.2,
            5, 2.0,
            7, 3.5,
          ],
          "circle-color": [
            "match", ["get", "shipType"],
            "cargo", "#3b82f6",
            "tanker", "#ef4444",
            "passenger", "#22c55e",
            "fishing", "#f59e0b",
            "pleasure", "#a855f7",
            "#888",
          ],
          "circle-opacity": [
            "interpolate", ["linear"], ["zoom"],
            2, 0.35,
            4, 0.65,
            6, 0.8,
            7.5, 0,
          ],
          "circle-stroke-width": 0,
        },
      });

      // Ship icon layer (zoom 6.5+) — fades in over dots
      if (m.getLayer("vessel-point")) m.removeLayer("vessel-point");
      m.addLayer({
        id: "vessel-point",
        type: "symbol",
        source: "vessels",
        filter: categoryFilter(activeCategories),
        minzoom: 6.5,
        layout: {
          "icon-image": iconImageExpr(),
          "icon-rotate": ["get", "heading"],
          "icon-rotation-alignment": "map",
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-size": [
            "interpolate", ["linear"], ["zoom"],
            7, 0.35,
            9, 0.50,
            12, 0.65,
          ],
        },
        paint: {
          "icon-opacity": [
            "interpolate", ["linear"], ["zoom"],
            6.5, 0,
            7.5, 0.9,
          ],
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



      // Trajectory arrow hover: show time
      const trajPopup = new ml.Popup({
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
            `<span class="traj-tooltip-text">${escapeHtml(formatTrajTime(f.properties.ts))}</span>`,
          )
          .addTo(m);
      });
      m.on("mouseleave", "vt-arrows", () => {
        trajPopup.remove();
        m.getCanvas().style.cursor = "";
      });

      // Vessel hover: tooltip
      const hoverPopup = new ml.Popup({
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
      m.on("mousemove", "vessel-dots", (e) => {
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
      m.on("mouseleave", "vessel-dots", () => {
        hoverPopup.remove();
        m.getCanvas().style.cursor = "";
      });

      // Vessel click: radius + trajectory + sidebar selection + popup
      const handleVesselClick = async (e: maplibregl.MapMouseEvent & { features?: maplibregl.MapGeoJSONFeature[] }) => {
        const f = e.features?.[0];
        if (!f) return;
        const p = f.properties;
        if (!p) return;

        const mmsi = p.id as number | undefined;
        if (!mmsi) return;

        const coords = (f.geometry as any).coordinates as [number, number];
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
          center: [coords[0], coords[1]],
          zoom: Math.max(m.getZoom(), 8),
          duration: 600,
        });

        // Show vessel detail popup
        // Construct vessel from feature properties (more reliable than searching displayVessels)
        const popupShipType = (p.shipType as string | undefined) ?? "pleasure";
        const popupMeta = VESSEL_META.find((mt) => mt.key === popupShipType);
        const popupColor = popupMeta?.color ?? "#888";

        // Build vessel object from feature properties
        const popupVessel: Vessel = {
          id: mmsi,
          name: p.name as string,
          lat: coords[1],
          lng: coords[0],
          heading: p.heading as number,
          speed: p.speed as number,
          shipType: popupShipType as ShipType,
          destination: p.destination as string | undefined,
          ts: p.ts as string | undefined,
        };

        // Close existing popup
        vesselDetailPopupRef.current?.remove();

        if (!vesselDetailPopupRef.current) {
          vesselDetailPopupRef.current = new ml.Popup({
            closeButton: true,
            closeOnClick: false,
            maxWidth: "300px",
            className: "vessel-detail-popup",
            offset: 10,
          });
        }

        const popup = vesselDetailPopupRef.current;
        const popupContainer = document.createElement("div");
        const popupRoot = createRoot(popupContainer);
        popupRoot.render(<VesselPopup vessel={popupVessel} color={popupColor} />);

        popup
          .setLngLat([coords[0], coords[1]])
          .setDOMContent(popupContainer)
          .addTo(m);

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
            .catch((err) => {
              console.error("[trajectory] queryVesselHistory failed:", err);
              if (gen !== trajectoryGenRef.current) return;
              setTrajectoryStatus("error");
            });
        }
      };

      m.on("click", "vessel-point", handleVesselClick);
      m.on("click", "vessel-dots", handleVesselClick);

      // Click off vessel/port → clear selection
      m.on("click", (e) => {
        const layers = showPortCongestion
          ? ["vessel-point", "vessel-dots", "ports-congestion"]
          : ["vessel-point", "vessel-dots"];
        if (
          m.queryRenderedFeatures(e.point, {
            layers,
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
        vesselDetailPopupRef.current?.remove();
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

  // Toggle port congestion layer
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !sourceReady || !maplibreglRuntime) return;
    
    const ml = maplibreglRuntime;

    if (showPortCongestion) {
      // Add source and layer if they don't exist
      if (!map.getSource("ports")) {
        map.addSource("ports", {
          type: "geojson",
          data: { type: "FeatureCollection", features: [] },
        });
      }
      if (!map.getLayer("ports-congestion")) {
        map.addLayer(
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
      }

      // Add hover popup if it doesn't exist
      if (!portHoverPopupRef.current) {
        portHoverPopupRef.current = new ml.Popup({
          closeButton: false,
          closeOnClick: false,
          offset: 8,
          className: "hover-tooltip",
        });
      }
      // Add click popup if it doesn't exist
      if (!portDetailPopupRef.current) {
        portDetailPopupRef.current = new ml.Popup({
          closeButton: true,
          closeOnClick: false,
          maxWidth: "320px",
          className: "port-detail-popup",
        });
      }

      // Add hover events
      map.on("mousemove", "ports-congestion", (e) => {
        const f = e.features?.[0];
        if (!f?.properties) return;
        map.getCanvas().style.cursor = "pointer";
        const p = f.properties;
        const popup = portHoverPopupRef.current;
        if (popup) {
          popup
            .setLngLat(e.lngLat)
            .setHTML(
              `<span class="hover-tooltip-text">${escapeHtml(p.port_name)} &middot; ${p.vessels_in_port} in port &middot; +${p.arrivals} / -${p.departures}</span>`,
            )
            .addTo(map);
        }
      });
      map.on("mouseleave", "ports-congestion", () => {
        portHoverPopupRef.current?.remove();
        map.getCanvas().style.cursor = "";
      });

      // Add click event
      map.on("click", "ports-congestion", async (e) => {
        const f = e.features?.[0];
        if (!f?.properties) return;
        const p = f.properties;
        const loCode = p.port_lo_code;
        const name = p.port_name;

        const popup = portDetailPopupRef.current;
        if (popup) {
          popup
            .setLngLat(e.lngLat)
            .setHTML(
              `<div class="port-popup-loading">Loading ${escapeHtml(name)}...</div>`,
            )
            .addTo(map);
        }

        try {
          const calls = await queryPortCalls(loCode, dateRef.current, 10);
          if (calls.length === 0) {
            portDetailPopupRef.current?.setHTML(
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
                  <span>MMSI ${escapeHtml(String(c.mmsi))}</span>
                  <span>${escapeHtml(c.arrival_ts?.slice(11, 19) ?? "?")}</span>
                  ${c.departure_ts ? `<span>&rarr; ${escapeHtml(c.departure_ts.slice(11, 19))}</span>` : '<span class="port-popup-active">in port</span>'}
                </div>`,
            )
            .join("");
          portDetailPopupRef.current?.setHTML(
            `<div class="port-popup">
              <strong>${escapeHtml(name)}</strong>
              <div class="port-popup-sub">${calls.length} calls (${p.vessels_in_port} in port, +${p.arrivals}/-${p.departures} last hour)</div>
              ${rows}
            </div>`,
          );
        } catch {
          portDetailPopupRef.current?.setHTML(
            `<div class="port-popup"><strong>${escapeHtml(name)}</strong><div class="port-popup-row">Failed to load calls</div></div>`,
          );
        }
      });
    } else {
      // Remove layer and source
      if (map.getLayer("ports-congestion")) {
        map.removeLayer("ports-congestion");
      }
      if (map.getSource("ports")) {
        map.removeSource("ports");
      }

      // Remove event listeners by removing and recreating popups
      portHoverPopupRef.current?.remove();
      portHoverPopupRef.current = null;
      portDetailPopupRef.current?.remove();
      portDetailPopupRef.current = null;
    }
  }, [showPortCongestion, sourceReady, dateRef, queryPortCalls]);

  // Update port congestion data on map
  useEffect(() => {
    if (!showPortCongestion) return;
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
  }, [ports, sourceReady, showPortCongestion]);

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
    const filter = categoryFilter(activeCategories);
    if (map.getLayer("vessel-dots")) map.setFilter("vessel-dots", filter);
    if (map.getLayer("vessel-point")) map.setFilter("vessel-point", filter);
    if (map.getLayer("vessel-label")) map.setFilter("vessel-label", filter);
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
          minzoom: 8,
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

    if (sat.tileUrl && !_scenesOnly) {
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
  }, [sat.tileUrl, sourceReady, _scenesOnly]);

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
    if (!map || !sourceReady || !maplibreglRuntime) return;
    
    const ml = maplibreglRuntime;

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

      const popup = new ml.Popup({
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
            `<span style="font:11px system-ui;color:var(--color-text)">${escapeHtml(f.properties.acquisition_time)}</span>`,
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
        showPortCongestion={showPortCongestion}
        onTogglePortCongestion={() => setShowPortCongestion((v) => !v)}
        width={sidebarWidth}
        onWidthChange={setSidebarWidth}
      />
      {!maplibreLoading && <div ref={mapContainer} className="map-container" />}
      {maplibreLoading && (
        <div className="map-container map-loading">
          <Spinner />
          Loading map library...
        </div>
      )}

      {/* Backdrop — mobile sidebar dismiss */}
      <div
        className={`sidebar-backdrop${!sidebarCollapsed ? " visible" : ""}`}
        onClick={() => setSidebarCollapsed(true)}
      />

      {/* Top bar */}
      <TopBar theme={theme} onToggleTheme={toggleTheme}>
        {maplibreLoading && (
          <StatusBadge type="info">
            <Spinner />
            Loading map library...
          </StatusBadge>
        )}
        {!ready && !maplibreLoading && (
          <StatusBadge type="info">
            <Spinner />
            Initializing DuckDB...
          </StatusBadge>
        )}

        {loading && (
          <StatusBadge type="loading">
            <Spinner />
            Loading...
          </StatusBadge>
        )}

        {error && (
          <StatusBadge type="error">{error}</StatusBadge>
        )}
      </TopBar>

      {/* Bottom area — timeline + floating elements */}
      <BottomBar
        timeline={timeline}
        vesselCount={displayVessels.length}
        legendVisible={legendVisible}
        onToggleLegend={() => setLegendVisible((v) => !v)}
        sensor={sensor}
        acquisitionTime={sat.acquisitionTime}
      />
    </div>
  );
}
