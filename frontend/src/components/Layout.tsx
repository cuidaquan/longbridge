import { useState, useEffect, ReactNode } from "react";
import Sidebar, { TabType } from "./Sidebar";

interface LayoutProps {
  children: (activeTab: TabType) => ReactNode;
}

const VALID_TABS = new Set<TabType>([
  "ai-trading",
  "smart-position",
  "stock-picker",
  "sector-rotation",
  "strategy-watch",
  "monitoring",
  "position-klines",
  "settings",
]);

export default function Layout({ children }: LayoutProps) {
  const [activeTab, setActiveTab] = useState<TabType>(() => {
    const saved = localStorage.getItem("activeTab");
    return saved && VALID_TABS.has(saved as TabType)
      ? (saved as TabType)
      : "ai-trading";
  });

  const [darkMode, setDarkMode] = useState(() => {
    return localStorage.getItem("darkMode") === "true";
  });

  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    return localStorage.getItem("sidebarCollapsed") === "true";
  });

  useEffect(() => {
    localStorage.setItem("activeTab", activeTab);
  }, [activeTab]);

  useEffect(() => {
    if (darkMode) {
      document.documentElement.classList.add("dark");
    } else {
      document.documentElement.classList.remove("dark");
    }
    localStorage.setItem("darkMode", String(darkMode));
  }, [darkMode]);

  useEffect(() => {
    localStorage.setItem("sidebarCollapsed", String(sidebarCollapsed));
  }, [sidebarCollapsed]);

  const toggleDarkMode = () => {
    setDarkMode(!darkMode);
  };

  return (
    <div className="min-h-screen bg-slate-100 dark:bg-slate-900 transition-colors duration-300">
      <Sidebar
        activeTab={activeTab}
        onTabChange={setActiveTab}
        darkMode={darkMode}
        onToggleDarkMode={toggleDarkMode}
        collapsed={sidebarCollapsed}
        onToggleCollapsed={() => setSidebarCollapsed((value) => !value)}
      />

      {/* Main Content */}
      <main
        className={`
          min-h-screen transition-all duration-300 ease-in-out
          ${sidebarCollapsed ? "ml-16" : "ml-16 md:ml-60"}
        `}
      >
        <div className="p-3 md:p-6 animate-fade-in">{children(activeTab)}</div>
      </main>
    </div>
  );
}

export type { TabType };
