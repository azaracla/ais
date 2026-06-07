import { useCallback, useEffect, useRef, useState } from "react";
import type maplibregl from "maplibre-gl";
import type { Bounds } from "./types";

export type DrawMode = "idle" | "drawing";

interface Point {
  lng: number;
  lat: number;
}

const CLICK_THRESHOLD = 5;

export function useDraw(map: maplibregl.Map | null) {
  const [mode, setMode] = useState<DrawMode>("idle");
  const [drawBounds, setDrawBounds] = useState<Bounds | null>(null);
  const startRef = useRef<Point | null>(null);
  const mouseDownRef = useRef<{ x: number; y: number } | null>(null);

  const srcId = "draw-rect";

  const renderRect = useCallback((p1: Point, p2: Point, preview: boolean) => {
    if (!map) return;
    const coords = [
      [p1.lng, p1.lat],
      [p2.lng, p1.lat],
      [p2.lng, p2.lat],
      [p1.lng, p2.lat],
      [p1.lng, p1.lat],
    ];

    if (!map.getSource(srcId)) {
      map.addSource(srcId, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: "draw-rect-fill",
        type: "fill",
        source: srcId,
        paint: {
          "fill-color": "#2563eb",
          "fill-opacity": preview ? 0.08 : 0.15,
        },
      });
      map.addLayer({
        id: "draw-rect-outline",
        type: "line",
        source: srcId,
        paint: {
          "line-color": "#2563eb",
          "line-width": preview ? 1 : 2,
          "line-dasharray": preview ? [2, 4] : [1, 0],
        },
      });
    }

    const src = map.getSource(srcId) as any;
    src.setData({
      type: "Feature",
      properties: {},
      geometry: { type: "Polygon", coordinates: [coords] },
    });
  }, [map]);

  const removeRect = useCallback(() => {
    if (!map) return;
    ["draw-rect-fill", "draw-rect-outline"].forEach((id) => {
      if (map.getLayer(id)) map.removeLayer(id);
    });
    if (map.getSource(srcId)) map.removeSource(srcId);
  }, [map]);

  const clear = useCallback(() => {
    startRef.current = null;
    setDrawBounds(null);
    setMode("idle");
    removeRect();
  }, [removeRect]);

  const startDraw = useCallback(() => {
    clear();
    startRef.current = null;
    setMode("drawing");
  }, [clear]);

  useEffect(() => {
    if (!map || mode !== "drawing") return;

    const onMouseDown = (e: maplibregl.MapMouseEvent) => {
      mouseDownRef.current = { x: e.point.x, y: e.point.y };
    };

    const onMouseUp = (e: maplibregl.MapMouseEvent) => {
      const down = mouseDownRef.current;
      if (!down) return;
      mouseDownRef.current = null;

      const dx = e.point.x - down.x;
      const dy = e.point.y - down.y;
      if (Math.sqrt(dx * dx + dy * dy) > CLICK_THRESHOLD) return;

      const { lng, lat } = e.lngLat;
      const start = startRef.current;

      if (!start) {
        startRef.current = { lng, lat };
        renderRect({ lng, lat }, { lng, lat }, true);
        return;
      }

      const p1 = start;
      const p2 = { lng, lat };
      renderRect(p1, p2, false);
      startRef.current = null;
      setMode("idle");

      const west = Math.min(p1.lng, p2.lng);
      const east = Math.max(p1.lng, p2.lng);
      const south = Math.min(p1.lat, p2.lat);
      const north = Math.max(p1.lat, p2.lat);
      setDrawBounds({ west, east, south, north });
    };

    const onMouseMove = (e: maplibregl.MapMouseEvent) => {
      if (!startRef.current) return;
      renderRect(startRef.current, { lng: e.lngLat.lng, lat: e.lngLat.lat }, true);
    };

    map.on("mousedown", onMouseDown as any);
    map.on("mouseup", onMouseUp as any);
    map.on("mousemove", onMouseMove as any);
    map.getCanvas().style.cursor = "crosshair";

    return () => {
      map.off("mousedown", onMouseDown as any);
      map.off("mouseup", onMouseUp as any);
      map.off("mousemove", onMouseMove as any);
      map.getCanvas().style.cursor = "";
    };
  }, [map, mode, renderRect]);

  return { mode, drawBounds, startDraw, clear, setDrawBounds };
}
