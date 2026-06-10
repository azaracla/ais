import type { ReactNode } from "react";

interface TopBarProps {
  children?: ReactNode;
}

export default function TopBar({ children }: TopBarProps) {
  return <div className="top-bar">{children}</div>;
}

interface StatusBadgeProps {
  type: "info" | "loading" | "error";
  children: ReactNode;
}

export function StatusBadge({ type, children }: StatusBadgeProps) {
  const className = `panel panel-md badge badge-${type}`;
  return <div className={className}>{children}</div>;
}

interface SpinnerProps {
  className?: string;
}

export function Spinner({ className = "spinner-sm" }: SpinnerProps) {
  return <span className={className} />;
}
