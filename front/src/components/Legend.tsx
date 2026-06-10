import { VESSEL_META } from "../constants/vesselMeta";

interface LegendProps {
  vesselCount: number;
  visible: boolean;
  onToggle: () => void;
}

export default function Legend({ vesselCount, visible, onToggle }: LegendProps) {
  return (
    <>
      <div className={`panel panel-lg legend${visible ? " mobile-visible" : ""}`}>
        <LegendContent vesselCount={vesselCount} />
      </div>
      
      {/* Legend toggle (mobile only) */}
      <button
        className={`panel panel-sm legend-toggle${visible ? " active" : ""}`}
        onClick={onToggle}
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
    </>
  );
}

function LegendContent({ vesselCount }: { vesselCount: number }) {
  return (
    <>
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
      <div className="legend-count">{vesselCount.toLocaleString()} vessels</div>
    </>
  );
}
