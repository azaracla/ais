import type { Vessel } from "./types";
import { navStatusLabel } from "./types";

interface Props {
  vessel: Vessel;
  color: string;
}

function formatTimeAgo(isoString: string | undefined): string {
  if (!isoString) return "";
  const date = new Date(isoString);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMin / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `il y a ${diffMin} min`;
  if (diffHours < 24) return `il y a ${diffHours}h`;
  return `il y a ${diffDays}j`;
}

export default function VesselPopup({ vessel, color }: Props) {
  const d = vessel;

  return (
    <div className="vessel-popup">
      <div className="vessel-popup-header">
        <span
          className="vessel-popup-color-bar"
          style={{ background: color }}
        />
        <span className="vessel-popup-name">{d.name}</span>
      </div>

      <div className="vessel-popup-body">
        <div className="vessel-popup-row">
          <span className="vessel-popup-label">Type</span>
          <span className="vessel-popup-value">
            {d.shipType ? d.shipType.charAt(0).toUpperCase() + d.shipType.slice(1) : "Unknown"}
          </span>
        </div>

        <div className="vessel-popup-row">
          <span className="vessel-popup-label">MMSI</span>
          <span className="vessel-popup-value mono">{d.id}</span>
        </div>

        {d.imo != null && (
          <div className="vessel-popup-row">
            <span className="vessel-popup-label">IMO</span>
            <span className="vessel-popup-value mono">{d.imo}</span>
          </div>
        )}

        {d.callSign && (
          <div className="vessel-popup-row">
            <span className="vessel-popup-label">Call Sign</span>
            <span className="vessel-popup-value mono">{d.callSign}</span>
          </div>
        )}

        <div className="vessel-popup-row">
          <span className="vessel-popup-label">Vitesse / Cap</span>
          <span className="vessel-popup-value">
            {d.speed.toFixed(1)} kn / {d.heading}°
          </span>
        </div>

        {d.destination && (
          <div className="vessel-popup-row">
            <span className="vessel-popup-label">Destination</span>
            <span className="vessel-popup-value">{d.destination}</span>
          </div>
        )}

        {d.navStatus != null && (
          <div className="vessel-popup-row">
            <span className="vessel-popup-label">Statut</span>
            <span className="vessel-popup-value">{navStatusLabel(d.navStatus)}</span>
          </div>
        )}

        {d.ts && (
          <div className="vessel-popup-row">
            <span className="vessel-popup-label">AIS</span>
            <span className="vessel-popup-value mono">
              {new Date(d.ts).toISOString().slice(0, 19).replace("T", " ")} UTC
            </span>
          </div>
        )}

        {d.ts && (
          <div className="vessel-popup-row vessel-popup-timeago">
            <span className="vessel-popup-value-dim">{formatTimeAgo(d.ts)}</span>
          </div>
        )}
      </div>
    </div>
  );
}
