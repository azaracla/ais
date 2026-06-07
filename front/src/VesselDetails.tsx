import { navStatusLabel } from "./types";
import type { Vessel } from "./types";

interface Props {
  vessel: Vessel;
  color: string;
  trajectoryStatus: "loading" | "done" | "error" | "idle";
  trajectoryCount: number;
}

export default function VesselDetails({
  vessel,
  color,
  trajectoryStatus,
  trajectoryCount,
}: Props) {
  const d = vessel;

  return (
    <div className="vd-wrap">
      <div className="vd-header">
        <span className="vd-color-bar" style={{ background: color }} />
        <span className="vd-name">{d.name}</span>
      </div>

      <div className="vd-grid">
        <span className="vd-label">MMSI</span>
        <span className="vd-value">{d.id}</span>

        {d.imo != null && (
          <>
            <span className="vd-label">IMO</span>
            <span className="vd-value">{d.imo}</span>
          </>
        )}

        {d.callSign && (
          <>
            <span className="vd-label">Call Sign</span>
            <span className="vd-value mono">{d.callSign}</span>
          </>
        )}

        <span className="vd-label">Type</span>
        <span className="vd-value" style={{ textTransform: "capitalize" }}>
          {d.shipType}
        </span>

        {d.length != null && d.width != null && isFinite(d.length) && isFinite(d.width) && (
          <>
            <span className="vd-label">Dimensions</span>
            <span className="vd-value">
              {d.length}m &times; {d.width}m
            </span>
          </>
        )}

        <span className="vd-label">Speed</span>
        <span className="vd-value">{d.speed.toFixed(1)} kn</span>

        <span className="vd-label">Heading</span>
        <span className="vd-value">{d.heading}&deg;</span>

        {d.navStatus != null && (
          <>
            <span className="vd-label">Status</span>
            <span className="vd-value">{navStatusLabel(d.navStatus)}</span>
          </>
        )}

        {d.destination && (
          <>
            <span className="vd-label">Destination</span>
            <span className="vd-value">{d.destination}</span>
          </>
        )}

        {d.ts && (
          <>
            <span className="vd-label">AIS Time</span>
            <span className="vd-value mono">
              {new Date(d.ts).toISOString().slice(0, 19).replace("T", " ")} UTC
            </span>
          </>
        )}

        {d.lastSeenStatic && (
          <>
            <span className="vd-label">Last Static</span>
            <span className="vd-value mono">
              {new Date(d.lastSeenStatic)
                .toISOString()
                .slice(0, 19)
                .replace("T", " ")}{" "}
              UTC
            </span>
          </>
        )}
      </div>

      <div className="vd-section">
        <div className="vd-section-title">Track</div>
        {trajectoryStatus === "loading" && (
          <div className="traj-status">
            <span className="spinner-sm" />
            Loading track…
          </div>
        )}
        {trajectoryStatus === "done" && (
          <div className="traj-status traj-status-done">
            {trajectoryCount >= 2
              ? `${trajectoryCount} track points`
              : "No track data"}
          </div>
        )}
        {trajectoryStatus === "error" && (
          <div className="traj-status traj-status-error">
            Track unavailable
          </div>
        )}
        {trajectoryStatus === "idle" && (
          <div className="traj-status traj-status-done">
            Click on map to load track
          </div>
        )}
      </div>
    </div>
  );
}
