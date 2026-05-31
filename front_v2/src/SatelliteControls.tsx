import type { Sensor } from "./types";
import type { SatelliteState } from "./useSatellite";

const SENSOR_LABELS: Record<Sensor, string> = {
  S1: "Sentinel-1 (Radar)",
  S2: "Sentinel-2 (Optical)",
};

interface Props {
  active: Sensor | null;
  onSensorChange: (s: Sensor | null) => void;
  date: string | null;
  onDateChange: (d: string | null) => void;
  sat: SatelliteState;
  hasDrawArea: boolean;
  scenesOnly: boolean;
  onScenesOnlyChange: (v: boolean) => void;
}

export default function SatelliteControls({ active, onSensorChange, date, onDateChange, sat, hasDrawArea, scenesOnly, onScenesOnlyChange }: Props) {
  return (
    <div style={{
      background: "rgba(255,255,255,0.95)", padding: "8px 14px",
      borderRadius: 8, boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
      font: "13px system-ui",
    }}>
      <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: active ? 8 : 0 }}>
        <span style={{ fontWeight: 600, marginRight: 4 }}>Satellite</span>
        {([null, "S2", "S1"] as const).map((s) => (
          <button
            key={s ?? "off"}
            onClick={() => onSensorChange(s)}
            style={{
              padding: "4px 10px", borderRadius: 4, border: "1px solid #ccc",
              cursor: "pointer", fontSize: 12,
              background: active === s ? "#6366f1" : "#fff",
              color: active === s ? "#fff" : "#333",
              fontWeight: active === s ? 600 : 400,
            }}
          >
            {s === null ? "Basemap" : SENSOR_LABELS[s]}
          </button>
        ))}
      </div>

      {active && (
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 4, cursor: "pointer" }}>
            <input type="checkbox" checked={scenesOnly} onChange={(e) => onScenesOnlyChange(e.target.checked)} />
            Scenes only
          </label>

          {hasDrawArea ? (
            <span style={{ fontSize: 11, color: "#6366f1" }}>Area drawn</span>
          ) : (
            <span style={{ fontSize: 11, color: "#888" }}>Using viewport — draw a polygon</span>
          )}

          {sat.loading && <span style={{ color: "#6366f1" }}>Loading dates...</span>}
          {sat.error && <span style={{ color: "#ef4444" }}>{sat.error}</span>}
          {!sat.loading && !sat.error && sat.dates.length === 0 && (
            <span style={{ color: "#888" }}>No images found for this area</span>
          )}
          {sat.dates.length > 0 && (
            <select
              value={date ?? ""}
              onChange={(e) => onDateChange(e.target.value || null)}
              style={{
                font: "13px system-ui", border: "1px solid #ccc",
                borderRadius: 4, padding: "3px 6px", maxWidth: 180,
              }}
            >
              <option value="">Auto (same day as AIS)</option>
              {sat.dates.map((d) => (
                <option key={d} value={d}>{d}</option>
              ))}
            </select>
          )}
          {!sat.loading && sat.dates.length > 0 && (
            <span style={{ color: "#888", fontSize: 11 }}>
              {sat.dates.length} date{sat.dates.length > 1 ? "s" : ""}
              {sat.scenes !== null && ` · ${sat.scenes.features.length} scene${sat.scenes.features.length > 1 ? "s" : ""}`}
            </span>
          )}
          {!sat.loading && sat.dates.length > 0 && sat.scenes !== null && sat.scenes.features.length === 0 && (
            <span style={{ color: "#f59e0b", fontSize: 11 }}>
              No scenes for this date
            </span>
          )}
        </div>
      )}
    </div>
  );
}
