import { useState, useEffect, useRef } from "react";
import { getVesselDetail } from "./duckdb";
import { navStatusLabel } from "./types";
import type { Vessel, VesselDetail } from "./types";

interface Props {
  mmsi: number;
  vessel: Vessel;
  color: string;
  trajectoryStatus: "loading" | "done" | "error" | "idle";
  trajectoryCount: number;
}

export default function VesselDetails({
  mmsi,
  vessel,
  color,
  trajectoryStatus,
  trajectoryCount,
}: Props) {
  const [detail, setDetail] = useState<VesselDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const genRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    const gen = ++genRef.current;
    setDetail(null);
    setLoading(true);
    getVesselDetail(mmsi)
      .then((d) => {
        if (cancelled || gen !== genRef.current) return;
        setDetail(d);
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled && gen === genRef.current) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [mmsi]);

  const d = detail ?? vessel;

  return (
    <div className="vd-wrap">
      <div className="vd-header">
        <span className="vd-color-bar" style={{ background: color }} />
        <span className="vd-name">{d.name}</span>
      </div>

      <div className="vd-grid">
        <span className="vd-label">MMSI</span>
        <span className="vd-value">{d.id}</span>

        {detail?.imo != null && (
          <>
            <span className="vd-label">IMO</span>
            <span className="vd-value">{detail.imo}</span>
          </>
        )}

        {detail?.callSign && (
          <>
            <span className="vd-label">Call Sign</span>
            <span className="vd-value mono">{detail.callSign}</span>
          </>
        )}

        <span className="vd-label">Type</span>
        <span className="vd-value" style={{ textTransform: "capitalize" }}>
          {d.shipType}
        </span>

        {detail?.length != null && detail?.width != null && isFinite(detail.length) && isFinite(detail.width) && (
          <>
            <span className="vd-label">Dimensions</span>
            <span className="vd-value">
              {detail.length}m &times; {detail.width}m
            </span>
          </>
        )}

        <span className="vd-label">Speed</span>
        <span className="vd-value">{d.speed.toFixed(1)} kn</span>

        <span className="vd-label">Heading</span>
        <span className="vd-value">{d.heading}&deg;</span>

        {detail?.navStatus != null && (
          <>
            <span className="vd-label">Status</span>
            <span className="vd-value">{navStatusLabel(detail.navStatus)}</span>
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

        {detail?.lastSeenStatic && (
          <>
            <span className="vd-label">Last Static</span>
            <span className="vd-value mono">
              {new Date(detail.lastSeenStatic)
                .toISOString()
                .slice(0, 19)
                .replace("T", " ")}{" "}
              UTC
            </span>
          </>
        )}
      </div>

      {loading && (
        <div className="vd-loading">
          <span className="spinner-sm" />
          Loading details…
        </div>
      )}

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
