import { useState, useRef, useCallback, useEffect } from "react";
import { queryPositionsAtTime, queryVesselWake, cancelQuery, isReady } from "./duckdb";
import type { Vessel, Bounds, WakePoint } from "./types";

interface UseTimelineReturn {
  currentTime: string;
  playing: boolean;
  speed: number;
  speedOptions: number[];
  isActive: boolean;
  timelineVessels: Vessel[];
  timelineLoading: boolean;
  wakeData: Map<number, WakePoint[]>;
  setPlaying: (p: boolean) => void;
  setSpeed: (s: number) => void;
  setCurrentTime: (t: string) => void;
  togglePlaying: () => void;
  getDayRange: () => { start: string; end: string };
}

function getInitialTimestamp(date: string): string {
  const d = new Date(date);
  const dayStart = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 0, 0, 0));
  const dayEnd = new Date(dayStart);
  dayEnd.setUTCHours(23, 59, 59, 999);
  if (d.getTime() < dayStart.getTime() || d.getTime() > dayEnd.getTime()) return date;
  return date;
}

export function useTimeline(date: string, bounds: Bounds | null, selectedMmsis: Set<number>): UseTimelineReturn {
  const speedOptions = [1, 2, 5, 10, 30, 60];

  const [rawCurrentTime, setRawCurrentTime] = useState(() => getInitialTimestamp(date));
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeedState] = useState(10);
  const [timelineVessels, setTimelineVessels] = useState<Vessel[]>([]);
  const [timelineLoading, setTimelineLoading] = useState(false);
  const [wakeData, setWakeData] = useState<Map<number, WakePoint[]>>(new Map());

  const currentTime = rawCurrentTime;
  const isActive = currentTime !== date;

  const genRef = useRef(0);
  const playingRef = useRef(false);
  const speedRef = useRef(speed);
  const boundsRef = useRef(bounds);
  const dateRef = useRef(date);
  const currentTimeRef = useRef(currentTime);

  useEffect(() => { speedRef.current = speed; }, [speed]);
  useEffect(() => { boundsRef.current = bounds; }, [bounds]);
  useEffect(() => { dateRef.current = date; }, [date]);

  const setTime = useCallback((t: string) => {
    currentTimeRef.current = t;
    setRawCurrentTime(t);
  }, []);

  const getDayRange = useCallback((): { start: string; end: string } => {
    const d = new Date(date);
    const start = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 0, 0, 0));
    const end = new Date(start);
    end.setUTCHours(23, 59, 59, 999);
    return { start: start.toISOString(), end: end.toISOString() };
  }, [date]);

  const loadPositions = useCallback(async (ts: string, b: Bounds | null, d: string) => {
    if (!isReady() || !b) return;
    const gen = ++genRef.current;
    setTimelineLoading(true);
    try {
      console.log(`[Timeline] loading positions at ${ts.slice(11,19)}`);
      const vessels = await queryPositionsAtTime(d, ts, b);
      if (gen !== genRef.current) return;
      console.log(`[Timeline] loaded ${vessels.length} vessels`);
      setTimelineVessels(vessels);
    } catch (e: any) {
      console.error(`[Timeline] load failed: ${e?.message ?? e}`);
    } finally {
      if (gen === genRef.current) setTimelineLoading(false);
    }
  }, []);

  const loadWake = useCallback(async (mmsis: number[], endTs: string) => {
    if (mmsis.length === 0 || !isReady()) return;
    const end = new Date(endTs);
    const start = new Date(end);
    start.setUTCHours(start.getUTCHours() - 2);
    try {
      const wake = await queryVesselWake(mmsis, start.toISOString(), endTs);
      setWakeData(wake);
    } catch {
      // ignore
    }
  }, []);

  useEffect(() => {
    if (!date) return;
    setTime(getInitialTimestamp(date));
    setPlaying(false);
    setTimelineVessels([]);
    setWakeData(new Map());
    playingRef.current = false;
    cancelQuery();
  }, [date]);

  useEffect(() => {
    if (!isActive || !bounds || playing) return;
    loadPositions(currentTime, bounds, date);
  }, [currentTime, isActive, bounds, date, playing, loadPositions]);

  useEffect(() => {
    if (!isActive || selectedMmsis.size === 0) {
      setWakeData(new Map());
      return;
    }
    const mmsis = Array.from(selectedMmsis);
    loadWake(mmsis, currentTime);
  }, [selectedMmsis, isActive, currentTime, loadWake]);

  const togglePlaying = useCallback(() => {
    setPlaying((p) => !p);
  }, []);

  useEffect(() => {
    if (!playing || !bounds) {
      playingRef.current = false;
      return;
    }

    console.log('[Timeline] ▶ animation started');
    playingRef.current = true;
    let ticker: ReturnType<typeof setInterval> | null = null;
    let pending = false;

    const tick = () => {
      if (!playingRef.current || pending) return;

      const cur = new Date(currentTimeRef.current);
      cur.setUTCMinutes(cur.getUTCMinutes() + speedRef.current);

      const dayEnd = new Date(dateRef.current);
      const dayStart = new Date(Date.UTC(dayEnd.getUTCFullYear(), dayEnd.getUTCMonth(), dayEnd.getUTCDate(), 0, 0, 0));
      const endOfDay = new Date(dayStart);
      endOfDay.setUTCHours(23, 59, 59, 999);

      if (cur >= endOfDay) {
        console.log('[Timeline] ■ reached end of day');
        setPlaying(false);
        playingRef.current = false;
        setTime(dayStart.toISOString());
        return;
      }

      const newTs = cur.toISOString();
      pending = true;
      const b = boundsRef.current;
      const d = dateRef.current;

      loadPositions(newTs, b, d).finally(() => {
        pending = false;
        setTime(newTs);
      });
    };

    ticker = setInterval(tick, 2000);
    tick(); // first tick immediately

    return () => {
      console.log('[Timeline] ■ animation stopped');
      playingRef.current = false;
      if (ticker) clearInterval(ticker);
    };
  }, [playing, bounds, loadPositions]);

  return {
    currentTime,
    playing,
    speed,
    speedOptions,
    isActive,
    timelineVessels,
    timelineLoading,
    wakeData,
    setPlaying,
    setSpeed: setSpeedState,
    setCurrentTime: setTime,
    togglePlaying,
    getDayRange,
  };
}
