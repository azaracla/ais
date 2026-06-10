import type { Sensor } from "./types";
import type { SatelliteState } from "./hooks/useSatellite";
import type { DrawMode } from "./hooks/useDraw";

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
  expanded: boolean;
  onToggleExpand: () => void;
  drawMode: DrawMode;
  onStartDraw: () => void;
  onClearDraw: () => void;
}

export default function SatelliteControls({
  active,
  onSensorChange,
  date,
  onDateChange,
  sat,
  hasDrawArea,
  scenesOnly,
  onScenesOnlyChange,
  expanded,
  onToggleExpand,
  drawMode,
  onStartDraw,
  onClearDraw,
}: Props) {
  if (!expanded) {
    return (
      <button
        className={`panel panel-md btn${active ? " btn-active" : ""}`}
        onClick={onToggleExpand}
        title="Satellite imagery"
      >
        🛰️ {active ? SENSOR_LABELS[active] : "Satellite"}
      </button>
    );
  }

  const drawing = drawMode === "drawing";

  return (
    <div className="panel panel-md sat-controls">
      <div className="sat-row">
        <span className="control-label">Satellite</span>
        {([null, "S2", "S1"] as const).map((s) => (
          <button
            key={s ?? "off"}
            className={`btn${active === s ? " btn-active" : ""}`}
            onClick={() => onSensorChange(s)}
          >
            {s === null ? "Basemap" : SENSOR_LABELS[s]}
          </button>
        ))}
        <button className="btn" onClick={onToggleExpand} title="Collapse">
          ✕
        </button>
      </div>

      <div className="sat-row">
        <span className="control-label">Area</span>
        <button
          className={`btn${drawing ? " btn-active" : ""}`}
          onClick={onStartDraw}
          disabled={drawing}
        >
          {drawing ? "Click 2 corners" : "Rectangle"}
        </button>
        <button
          className="btn"
          onClick={onClearDraw}
          disabled={!hasDrawArea && !drawing}
        >
          Clear
        </button>
      </div>

      {active && (
        <div className="sat-row">
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={scenesOnly}
              onChange={(e) => onScenesOnlyChange(e.target.checked)}
            />
            Scenes only
          </label>

          {hasDrawArea ? (
            <span className="badge-dim" style={{ color: "var(--color-accent)" }}>
              Area drawn
            </span>
          ) : (
            <span className="badge-dim">Using viewport — draw a polygon</span>
          )}

          {sat.loading && (
            <span className="badge badge-loading">
              <span className="spinner-sm" />
              Loading dates...
            </span>
          )}
          {sat.error && (
            <span className="badge badge-error">{sat.error}</span>
          )}
          {!sat.loading && !sat.error && sat.dates.length === 0 && (
            <span className="badge-dim">No images found for this area</span>
          )}
          {sat.dates.length > 0 && (
            <select
              className="input-text"
              value={date ?? ""}
              onChange={(e) => onDateChange(e.target.value || null)}
            >
              <option value="">Auto (same day as AIS)</option>
              {sat.dates.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          )}
          {!sat.loading && sat.dates.length > 0 && (
            <span className="badge-dim">
              {sat.dates.length} date{sat.dates.length > 1 ? "s" : ""}
              {sat.scenes !== null &&
                ` · ${sat.scenes.features.length} scene${sat.scenes.features.length > 1 ? "s" : ""}`}
            </span>
          )}
          {!sat.loading &&
            sat.dates.length > 0 &&
            sat.scenes !== null &&
            sat.scenes.features.length === 0 && (
              <span className="badge badge-warning">
                No scenes for this date
              </span>
            )}
        </div>
      )}
    </div>
  );
}
