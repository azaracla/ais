import { useCallback, useEffect, useRef, useState } from "react";
import type { Bounds, Sensor } from "./types";

const API_BASE = import.meta.env.VITE_SATELLITE_PROXY_URL ?? "http://localhost:8000";

function boundsToBboxStr(b: Bounds): string {
  return `${b.west},${b.south},${b.east},${b.north}`;
}

export interface SatelliteState {
  dates: string[];
  loading: boolean;
  error: string | null;
  tileUrl: string | null;
  acquisitionTime: string | null;
  scenes: GeoJSON.FeatureCollection | null;
}

export function useSatellite(
  sensor: Sensor | null,
  bounds: Bounds | null,
  date: string | null,
): SatelliteState {
  const [dates, setDates] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tileUrl, setTileUrl] = useState<string | null>(null);
  const [acquisitionTime, setAcquisitionTime] = useState<string | null>(null);
  const [scenes, setScenes] = useState<GeoJSON.FeatureCollection | null>(null);
  const genRef = useRef(0);

  const fetchDates = useCallback(async (s: Sensor, b: Bounds) => {
    const gen = ++genRef.current;
    setLoading(true);
    setError(null);

    const bbox = boundsToBboxStr(b);
    const url = `${API_BASE}/map?sensor=${s}&bbox=${bbox}`;

    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (gen !== genRef.current) return;
      setDates(data.dates ?? []);
    } catch (e: any) {
      if (gen !== genRef.current) return;
      setError(e.message ?? "Failed to fetch dates");
    } finally {
      if (gen === genRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!sensor || !bounds) {
      setDates([]);
      setTileUrl(null);
      setAcquisitionTime(null);
      setScenes(null);
      setError(null);
      return;
    }
    fetchDates(sensor, bounds);
  }, [sensor, bounds, fetchDates]);

  useEffect(() => {
    if (!sensor || !bounds || !date) {
      setTileUrl(null);
      setAcquisitionTime(null);
      setScenes(null);
      return;
    }

    const end = new Date(date);
    end.setDate(end.getDate() + 1);
    const endStr = end.toISOString().slice(0, 10);

    const bbox = boundsToBboxStr(bounds);
    const url = `${API_BASE}/tiles/{z}/{x}/{y}?sensor=${sensor}&bbox=${bbox}&start=${date}&end=${endStr}`;
    setTileUrl(url);

    const bboxQ = bbox;
    fetch(`${API_BASE}/acquisition-time?date=${date}&sensor=${sensor}&bbox=${bboxQ}`)
      .then((r) => r.json())
      .then((d) => { if (d.acquisition_time) setAcquisitionTime(d.acquisition_time); })
      .catch(() => {});

    fetch(`${API_BASE}/scenes?date=${date}&sensor=${sensor}&bbox=${bboxQ}`)
      .then((r) => r.json())
      .then((d) => {
        if (d && d.features) {
          d.features.sort((a: any, b: any) => {
            const ta = a.properties?.acquisition_time ?? "";
            const tb = b.properties?.acquisition_time ?? "";
            return ta < tb ? -1 : ta > tb ? 1 : 0;
          });
          setScenes(d as GeoJSON.FeatureCollection);
        }
      })
      .catch(() => {});
  }, [sensor, bounds, date]);

  return { dates, loading, error, tileUrl, acquisitionTime, scenes };
}
