import type { ReactNode } from "react";
import ThemeToggle from "./ThemeToggle";

interface TopBarProps {
  children?: ReactNode;
  theme?: "light" | "dark";
  onToggleTheme?: () => void;
}

export default function TopBar({ children, theme = "light", onToggleTheme }: TopBarProps) {
  return (
    <div className="top-bar">
      {children}
      {onToggleTheme && (
        <ThemeToggle theme={theme} onToggle={onToggleTheme} />
      )}
    </div>
  );
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
