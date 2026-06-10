import type { ReactNode } from "react";
import Timeline from "../Timeline";
import Legend from "./Legend";
import type { UseTimelineReturn } from "../hooks/useTimeline";

interface BottomBarProps {
  timeline: UseTimelineReturn;
  vesselCount: number;
  legendVisible: boolean;
  onToggleLegend: () => void;
  sensor?: string | null | undefined;
  acquisitionTime?: string | null | undefined;
  children?: ReactNode;
}

export default function BottomBar({
  timeline,
  vesselCount,
  legendVisible,
  onToggleLegend,
  sensor,
  acquisitionTime,
  children,
}: BottomBarProps) {
  return (
    <div className="bottom-bar">
      <Timeline
        currentTime={timeline.currentTime}
        playing={timeline.playing}
        speed={timeline.speed}
        speedOptions={timeline.speedOptions}
        isActive={timeline.isActive}
        loading={timeline.timelineLoading}
        date={timeline.isActive ? timeline.currentTime : timeline.date}
        onDateChange={timeline.onDateChange}
        onTogglePlay={timeline.togglePlaying}
        onSpeedChange={timeline.setSpeed}
        onScrub={timeline.setCurrentTime}
        getDayRange={timeline.getDayRange}
      />

      {/* Acquisition time badge */}
      {sensor && acquisitionTime && (
        <div className="acq-badge">
          {sensor === "S1" ? "Sentinel-1" : "Sentinel-2"} · {acquisitionTime}
        </div>
      )}

      <Legend
        vesselCount={vesselCount}
        visible={legendVisible}
        onToggle={onToggleLegend}
      />
      {children}
    </div>
  );
}
