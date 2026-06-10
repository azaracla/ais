import type { Vessel } from "../../types";
import { CAT_COLORS } from "../../constants/sidebar";

interface VesselRowProps {
  vessel: Vessel;
  isSelected: boolean;
  onClick: () => void;
  style?: React.CSSProperties;
}

export default function VesselRow({ vessel, isSelected, onClick, style }: VesselRowProps) {
  const color = CAT_COLORS[vessel.shipType];

  return (
    <div
      style={style}
      onClick={onClick}
      className={`sidebar-item${isSelected ? " selected" : ""}`}
    >
      <span
        className="sidebar-item-icon"
        style={{ background: color }}
      />
      <span className="sidebar-item-body">
        <span className="sidebar-item-name">{vessel.name}</span>
        {vessel.destination && (
          <span className="sidebar-item-dest">{vessel.destination}</span>
        )}
      </span>
      <span className="sidebar-item-right">
        <span className="sidebar-item-speed">
          {vessel.speed.toFixed(1)}
          <span className="sidebar-item-unit">kn</span>
        </span>
        <span className="sidebar-item-heading">
          {vessel.heading}&deg;
        </span>
      </span>
    </div>
  );
}
