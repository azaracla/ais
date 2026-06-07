import { useState, useMemo, useCallback, useEffect, useRef } from "react";
import { useVesselSearch } from "./useVesselSearch";
import VesselDetails from "./VesselDetails";
import type { Vessel, VesselSummary, ShipType } from "./types";

const SHIP_TYPE_KEYS: ShipType[] = [
  "cargo",
  "tanker",
  "passenger",
  "fishing",
  "pleasure",
];

const CAT_COLORS: Record<ShipType, string> = {
  cargo: "#3b82f6",
  tanker: "#ef4444",
  passenger: "#22c55e",
  fishing: "#f59e0b",
  pleasure: "#a855f7",
};

const SORT_OPTIONS = [
  { key: "speed", label: "Speed" },
  { key: "name", label: "Name" },
  { key: "type", label: "Type" },
] as const;

const MAX_RENDER = 500;

interface Props {
  vessels: Vessel[];
  loading: boolean;
  error: string | null;
  selectedMmsi: number | null;
  onSelectVessel: (mmsi: number) => void;
  onBack: () => void;
  collapsed: boolean;
  onToggleCollapse: () => void;
  activeCategories: Set<ShipType>;
  onToggleCategory: (cat: ShipType) => void;
  trajectoryStatus: "loading" | "done" | "error" | "idle";
  trajectoryCount: number;
  speedRange: [number, number];
  onSpeedRangeChange: (range: [number, number]) => void;
  showLabels: boolean;
  onToggleLabels: () => void;
}

export default function Sidebar({
  vessels,
  loading,
  error,
  selectedMmsi,
  onSelectVessel,
  onBack,
  collapsed,
  onToggleCollapse,
  activeCategories,
  onToggleCategory,
  trajectoryStatus,
  trajectoryCount,
  speedRange,
  onSpeedRangeChange,
  showLabels,
  onToggleLabels,
}: Props) {
  const [searchQuery, setSearchQuery] = useState("");
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [sortKey, setSortKey] = useState<string>("speed");
  const [suggestions, setSuggestions] = useState<VesselSummary[]>([]);
  const { search, loading: searchLoading } = useVesselSearch();
  const suggestGenRef = useRef(0);

  useEffect(() => {
    if (searchQuery.trim().length < 2) {
      setSuggestions([]);
      return;
    }
    const gen = ++suggestGenRef.current;
    const timer = setTimeout(() => {
      search(searchQuery, 10).then((results) => {
        if (gen === suggestGenRef.current) setSuggestions(results);
      });
    }, 200);
    return () => clearTimeout(timer);
  }, [searchQuery, search]);

  const filteredVessels = useMemo(() => {
    let list = vessels;
    if (activeCategories.size < 5) {
      list = list.filter((v) => activeCategories.has(v.shipType));
    }
    list = list.filter(
      (v) => v.speed >= speedRange[0] && v.speed <= speedRange[1],
    );
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      list = list.filter(
        (v) =>
          v.name.toLowerCase().includes(q) ||
          String(v.id).includes(q),
      );
    }
    list = [...list].sort((a, b) => {
      switch (sortKey) {
        case "name":
          return a.name.localeCompare(b.name);
        case "type":
          return a.shipType.localeCompare(b.shipType);
        case "speed":
        default:
          return b.speed - a.speed;
      }
    });
    return list;
  }, [vessels, activeCategories, searchQuery, sortKey, speedRange]);

  const renderedVessels = useMemo(
    () => filteredVessels.slice(0, MAX_RENDER),
    [filteredVessels],
  );

  const categoryCounts = useMemo(() => {
    const counts: Record<ShipType, number> = {
      cargo: 0,
      tanker: 0,
      passenger: 0,
      fishing: 0,
      pleasure: 0,
    };
    for (const v of vessels) {
      counts[v.shipType]++;
    }
    return counts;
  }, [vessels]);

  const maxSpeed = useMemo(() => {
    let max = 50;
    for (const v of vessels) {
      if (v.speed > max) max = Math.ceil(v.speed);
    }
    return max;
  }, [vessels]);

  const selectedVessel = useMemo(
    () => vessels.find((v) => v.id === selectedMmsi) ?? null,
    [vessels, selectedMmsi],
  );

  const handleSuggestionClick = useCallback(
    (mmsi: number) => {
      setSearchQuery("");
      setShowSuggestions(false);
      setSuggestions([]);
      onSelectVessel(mmsi);
    },
    [onSelectVessel],
  );

  const handleSearchKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && suggestions.length > 0) {
        handleSuggestionClick(suggestions[0].mmsi);
      }
    },
    [suggestions, handleSuggestionClick],
  );

  const isDetailMode = selectedMmsi !== null && selectedVessel !== null;

  return (
    <>
      {collapsed && (
        <button className="sidebar-toggle" onClick={onToggleCollapse}>
          <svg width="16" height="16" viewBox="0 0 16 16">
            <path d="M10 3L5 8l5 5" stroke="currentColor" strokeWidth="2" fill="none" />
          </svg>
        </button>
      )}

      <div className={`sidebar${collapsed ? " collapsed" : ""}`}>
        <div className="sidebar-inner">
          {isDetailMode ? (
            <>
              <div className="sidebar-back-row">
                <button className="sidebar-back" onClick={onBack}>
                  <svg width="14" height="14" viewBox="0 0 16 16">
                    <path d="M10 3L5 8l5 5" stroke="currentColor" strokeWidth="2" fill="none" />
                  </svg>
                  Back to list
                </button>
                <button className="sidebar-collapse-btn" onClick={onToggleCollapse}>
                  <svg width="14" height="14" viewBox="0 0 16 16">
                    <path d="M6 3L11 8l-5 5" stroke="currentColor" strokeWidth="2" fill="none" />
                  </svg>
                </button>
              </div>
              <VesselDetails
                vessel={selectedVessel!}
                color={CAT_COLORS[selectedVessel!.shipType]}
                trajectoryStatus={trajectoryStatus}
                trajectoryCount={trajectoryCount}
              />
            </>
          ) : (
            <>
              <div className="sidebar-header">
                <span className="sidebar-title">
                  Vessels{" "}
                  <span className="sidebar-count">
                    {vessels.length.toLocaleString()}
                  </span>
                </span>
                <button className="sidebar-collapse-btn" onClick={onToggleCollapse}>
                  <svg width="14" height="14" viewBox="0 0 16 16">
                    <path d="M6 3L11 8l-5 5" stroke="currentColor" strokeWidth="2" fill="none" />
                  </svg>
                </button>
              </div>

              <div className="sidebar-search-wrap">
                <input
                  className="sidebar-search"
                  type="text"
                  placeholder="Search vessel name or MMSI…"
                  value={searchQuery}
                  onChange={(e) => {
                    setSearchQuery(e.target.value);
                    setShowSuggestions(e.target.value.trim().length >= 2);
                  }}
                  onFocus={() =>
                    setShowSuggestions(searchQuery.trim().length >= 2)
                  }
                  onBlur={() =>
                    setTimeout(() => setShowSuggestions(false), 200)
                  }
                  onKeyDown={handleSearchKeyDown}
                />
                {showSuggestions && (
                  <div className="sidebar-suggestions">
                    {searchLoading && (
                      <div className="sidebar-suggestion-item" style={{ color: "var(--color-text-dim)" }}>
                        <span className="spinner-sm" /> Searching…
                      </div>
                    )}
                    {!searchLoading && suggestions.length === 0 && searchQuery.trim().length >= 2 && (
                      <div className="sidebar-suggestion-item" style={{ color: "var(--color-text-dim)" }}>
                        No vessels found
                      </div>
                    )}
                    {suggestions.map((s) => (
                      <button
                        key={s.mmsi}
                        className="sidebar-suggestion-item"
                        onMouseDown={(e) => e.preventDefault()}
                        onClick={() => handleSuggestionClick(s.mmsi)}
                      >
                        <span
                          className="sidebar-suggestion-dot"
                          style={{ background: CAT_COLORS[s.shipType] }}
                        />
                        <span className="sidebar-suggestion-name">{s.name}</span>
                        <span className="sidebar-suggestion-mmsi">{s.mmsi}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>

              <div className="sidebar-filters">
                <button
                  className={`sidebar-chip${activeCategories.size === 5 ? " active" : ""}`}
                  onClick={() => {
                    if (activeCategories.size === 5) return;
                    for (const k of SHIP_TYPE_KEYS) onToggleCategory(k);
                  }}
                >
                  All
                </button>
                {SHIP_TYPE_KEYS.map((key) => (
                  <button
                    key={key}
                    className={`sidebar-chip${activeCategories.has(key) ? " active" : ""}`}
                    style={{
                      "--chip-color": CAT_COLORS[key],
                    } as React.CSSProperties}
                    onClick={() => onToggleCategory(key)}
                  >
                    <span
                      className="sidebar-chip-dot"
                      style={{ background: CAT_COLORS[key] }}
                    />
                    {key.charAt(0).toUpperCase() + key.slice(1)}
                    <span className="sidebar-chip-count">
                      {categoryCounts[key]}
                    </span>
                  </button>
                ))}
              </div>

              <div className="sidebar-speed">
                <div className="sidebar-speed-header">
                  <span className="sidebar-speed-label">
                    Speed: {speedRange[0]}–{speedRange[1]} kn
                  </span>
                  <label className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={showLabels}
                      onChange={onToggleLabels}
                    />
                    Labels
                  </label>
                </div>
                <div className="sidebar-speed-sliders">
                  <input
                    type="range"
                    className="speed-range"
                    min={0}
                    max={maxSpeed}
                    value={speedRange[0]}
                    onChange={(e) =>
                      onSpeedRangeChange([
                        Math.min(Number(e.target.value), speedRange[1]),
                        speedRange[1],
                      ])
                    }
                  />
                  <input
                    type="range"
                    className="speed-range"
                    min={0}
                    max={maxSpeed}
                    value={speedRange[1]}
                    onChange={(e) =>
                      onSpeedRangeChange([
                        speedRange[0],
                        Math.max(Number(e.target.value), speedRange[0]),
                      ])
                    }
                  />
                </div>
              </div>

              <div className="sidebar-sort">
                <span className="sidebar-sort-label">Sort:</span>
                {SORT_OPTIONS.map((opt) => (
                  <button
                    key={opt.key}
                    className={`sidebar-sort-btn${sortKey === opt.key ? " active" : ""}`}
                    onClick={() => setSortKey(opt.key)}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>

              {loading && (
                <div className="sidebar-status">
                  <span className="spinner-sm" />
                  Loading vessels…
                </div>
              )}
              {error && (
                <div className="sidebar-status sidebar-status-error">
                  {error}
                </div>
              )}

              <div className="sidebar-list">
                {renderedVessels.map((v) => (
                  <button
                    key={v.id}
                    className={`sidebar-item${
                      selectedMmsi === v.id ? " selected" : ""
                    }`}
                    onClick={() => onSelectVessel(v.id)}
                  >
                    <span
                      className="sidebar-item-icon"
                      style={{ background: CAT_COLORS[v.shipType] }}
                    />
                    <span className="sidebar-item-body">
                      <span className="sidebar-item-name">{v.name}</span>
                      {v.destination && (
                        <span className="sidebar-item-dest">
                          {v.destination}
                        </span>
                      )}
                    </span>
                    <span className="sidebar-item-right">
                      <span className="sidebar-item-speed">
                        {v.speed.toFixed(1)}
                        <span className="sidebar-item-unit">kn</span>
                      </span>
                      <span className="sidebar-item-heading">
                        {v.heading}&deg;
                      </span>
                    </span>
                  </button>
                ))}
                {filteredVessels.length === 0 && !loading && !error && (
                  <div className="sidebar-empty">
                    {vessels.length === 0
                      ? "No vessels in viewport"
                      : "No vessels match filters"}
                  </div>
                )}
              </div>

              <div className="sidebar-footer">
                {filteredVessels.length > renderedVessels.length
                  ? `Showing ${renderedVessels.length.toLocaleString()} of ${filteredVessels.length.toLocaleString()} (scroll to map or filter)`
                  : filteredVessels.length < vessels.length
                    ? `Showing ${filteredVessels.length.toLocaleString()} of ${vessels.length.toLocaleString()}`
                    : `${vessels.length.toLocaleString()} vessels`}
              </div>
            </>
          )}
        </div>
      </div>
    </>
  );
}
