import { ICON_SIZE, ARROW_SIZE, VESSEL_META } from "../constants/vesselMeta";
import { lightenColor } from "./colorUtils";

/**
 * Draw a ship icon with the given color, size, and theme
 */
export function drawShipIcon(
  color: string,
  size: number,
  theme: "light" | "dark",
): ImageData {
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d")!;
  const cx = size / 2;
  const cy = size / 2;
  const s = size; // use full canvas

  ctx.save();
  ctx.translate(cx, cy);

  // Glow
  const glow = ctx.createRadialGradient(0, 0, s * 0.08, 0, 0, s * 0.55);
  const glowAlpha = theme === "dark" ? 0.35 : 0.18;
  glow.addColorStop(0, color + Math.round(glowAlpha * 255).toString(16).padStart(2, "0"));
  glow.addColorStop(1, "transparent");
  ctx.fillStyle = glow;
  ctx.beginPath();
  ctx.arc(0, 0, s * 0.55, 0, Math.PI * 2);
  ctx.fill();

  // Hull — elongated ellipse pointing up (heading = up on canvas)
  const hullLen = s * 0.4;
  const hullW = s * 0.22;
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.ellipse(0, s * 0.06, hullW, hullLen, 0, 0, Math.PI * 2);
  ctx.fill();

  // Bow point (nose at top)
  ctx.beginPath();
  ctx.moveTo(0, -(s * 0.44));
  ctx.lineTo(-hullW * 0.5, -(hullLen * 0.4));
  ctx.lineTo(hullW * 0.5, -(hullLen * 0.4));
  ctx.closePath();
  ctx.fill();

  // Superstructure (bridge)
  ctx.fillStyle = lightenColor(color, 0.25);
  ctx.beginPath();
  ctx.roundRect(-hullW * 0.45, -hullLen * 0.5, hullW * 0.9, hullLen * 0.55, s * 0.08);
  ctx.fill();

  // Mast (vertical line)
  ctx.strokeStyle = color;
  ctx.lineWidth = Math.max(1, s * 0.06);
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(0, -s * 0.44);
  ctx.lineTo(0, -s * 0.48);
  ctx.stroke();

  // Outline for definition
  ctx.strokeStyle = theme === "dark" ? "rgba(255,255,255,0.2)" : "rgba(0,0,0,0.25)";
  ctx.lineWidth = 0.8;
  ctx.beginPath();
  ctx.ellipse(0, s * 0.06, hullW, hullLen, 0, 0, Math.PI * 2);
  ctx.moveTo(0, -(s * 0.44));
  ctx.lineTo(-hullW * 0.5, -(hullLen * 0.4));
  ctx.lineTo(hullW * 0.5, -(hullLen * 0.4));
  ctx.closePath();
  ctx.stroke();

  ctx.restore();
  return ctx.getImageData(0, 0, size, size);
}

/**
 * Create an arrow icon for trajectory direction
 */
export function makeArrowIcon(color: string, theme: "light" | "dark"): ImageData {
  const s = ARROW_SIZE;
  const canvas = document.createElement("canvas");
  canvas.width = s;
  canvas.height = s;
  const ctx = canvas.getContext("2d")!;
  const cx = s / 2;
  const cy = s / 2;

  // Arrow pointing up (0° = north)
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.moveTo(cx, cy - s * 0.45);          // tip
  ctx.lineTo(cx + s * 0.4, cy + s * 0.45); // bottom-right
  ctx.lineTo(cx + s * 0.12, cy + s * 0.15);
  ctx.lineTo(cx - s * 0.12, cy + s * 0.15);
  ctx.lineTo(cx - s * 0.4, cy + s * 0.45); // bottom-left
  ctx.closePath();
  ctx.fill();

  // Contrasting outline for visibility on both light and dark backgrounds
  ctx.strokeStyle = theme === "dark" ? "rgba(255,255,255,0.9)" : "rgba(0,0,0,0.6)";
  ctx.lineWidth = 1;
  ctx.stroke();

  return ctx.getImageData(0, 0, s, s);
}

/**
 * Pre-register all ship icons for a given theme
 */
export function registerShipIcons(map: maplibregl.Map, theme: "light" | "dark"): void {
  for (const meta of VESSEL_META) {
    const id = `ship-${meta.key}`;
    if (map.hasImage(id)) map.removeImage(id);
    map.addImage(id, drawShipIcon(meta.color, ICON_SIZE, theme));
  }
}

/**
 * Register arrow icon for trajectory
 */
export function registerArrowIcon(map: maplibregl.Map, color: string, theme: "light" | "dark", id: string = "traj-arrow"): void {
  if (map.hasImage(id)) map.removeImage(id);
  map.addImage(id, makeArrowIcon(color, theme));
}
