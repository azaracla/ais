import { useCallback, useMemo } from "react";

interface Props {
  currentTime: string;
  playing: boolean;
  speed: number;
  speedOptions: number[];
  isActive: boolean;
  loading: boolean;
  date: string;
  onDateChange: (date: string) => void;
  onTogglePlay: () => void;
  onSpeedChange: (s: number) => void;
  onScrub: (t: string) => void;
  getDayRange: () => { start: string; end: string };
}

function timeToFraction(ts: string, dayStart: Date): number {
  const t = new Date(ts).getTime();
  const d0 = dayStart.getTime();
  return Math.max(0, Math.min(1, (t - d0) / (24 * 60 * 60 * 1000)));
}

function fractionToTime(fraction: number, dayStart: Date): string {
  const ms = dayStart.getTime() + fraction * 24 * 60 * 60 * 1000;
  return new Date(ms).toISOString();
}


export default function Timeline({
  currentTime,
  playing,
  speed,
  speedOptions,
  isActive,
  loading,
  date,
  onDateChange,
  onTogglePlay,
  onSpeedChange,
  onScrub,
  getDayRange,
}: Props) {
  const dayStart = useMemo(() => {
    const { start } = getDayRange();
    return new Date(start);
  }, [getDayRange]);

  const fraction = timeToFraction(currentTime, dayStart);

  const handleScrub = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const f = parseFloat(e.target.value);
      onScrub(fractionToTime(f, dayStart));
    },
    [onScrub, dayStart],
  );

  const handleDateChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      onDateChange(new Date(e.target.value + "Z").toISOString());
    },
    [onDateChange],
  );

  const handleReset = useCallback(() => {
    const now = new Date();
    onScrub(now.toISOString());
  }, [onScrub]);

  return (
    <div className={`timeline-bar${isActive ? " active" : ""}`}>
      {/* Top row: date/time — "what & when" */}
      <div className="tl-top">
        <input
          type="datetime-local"
          className="date-input input-text"
          value={date.slice(0, 16)}
          onChange={handleDateChange}
        />
        {isActive && (
          <button
            className="timeline-btn timeline-reset-btn"
            onClick={handleReset}
            title="Back to live"
          >
            <span className="tl-live-dot" />
            Live
          </button>
        )}
      </div>

      {/* Bottom row: transport controls + scrubber — "playback" */}
      <div className="tl-bot">
        <button
          className="timeline-btn timeline-play-btn"
          onClick={onTogglePlay}
          title={playing ? "Pause" : "Play"}
        >
          {playing ? (
            <svg width="18" height="18" viewBox="0 0 18 18">
              <rect x="2" y="2" width="5" height="14" rx="1" fill="currentColor" />
              <rect x="11" y="2" width="5" height="14" rx="1" fill="currentColor" />
            </svg>
          ) : (
            <svg width="18" height="18" viewBox="0 0 18 18">
              <polygon points="4,2 16,9 4,16" fill="currentColor" />
            </svg>
          )}
        </button>

        <select
          className="timeline-speed"
          value={speed}
          onChange={(e) => onSpeedChange(Number(e.target.value))}
          title="Playback speed"
        >
          {speedOptions.map((s) => (
            <option key={s} value={s}>
              {s}x
            </option>
          ))}
        </select>

        <div className="timeline-scrub-wrap">
          <input
            type="range"
            className="timeline-scrub"
            min={0}
            max={1}
            step={0.0001}
            value={fraction}
            onChange={handleScrub}
          />
          <div className="timeline-ticks">
            <span>0h</span>
            <span>6h</span>
            <span>12h</span>
            <span>18h</span>
            <span>24h</span>
          </div>
        </div>

        <span className="timeline-spinner-wrap">
          {loading && <span className="spinner-sm" />}
        </span>
      </div>
    </div>
  );
}
