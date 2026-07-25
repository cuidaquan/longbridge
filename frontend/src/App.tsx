import { lazy, Suspense } from "react";
import Layout, { TabType } from "./components/Layout";

const SettingsPage = lazy(() => import("./pages/Settings"));
const PositionMonitoringPage = lazy(() => import("./pages/PositionMonitoring"));
const StrategyWatchPage = lazy(() => import("./pages/StrategyWatch"));
const PositionKLinesPage = lazy(() => import("./pages/PositionKLines"));
const SmartPositionPage = lazy(() => import("./pages/SmartPosition"));
const AiTradingPage = lazy(() => import("./pages/AiTrading"));
const StockPickerPage = lazy(() => import("./pages/StockPicker"));
const QuantStockSelectorPage = lazy(() => import("./pages/QuantStockSelector"));
const SectorRotationPage = lazy(() => import("./pages/SectorRotation"));

function renderPage(activeTab: TabType) {
  switch (activeTab) {
    case "ai-trading":
      return <AiTradingPage />;
    case "smart-position":
      return <SmartPositionPage />;
    case "stock-picker":
      return <StockPickerPage />;
    case "quant-stock-selector":
      return <QuantStockSelectorPage />;
    case "sector-rotation":
      return <SectorRotationPage />;
    case "strategy-watch":
      return <StrategyWatchPage />;
    case "monitoring":
      return <PositionMonitoringPage />;
    case "position-klines":
      return <PositionKLinesPage />;
    case "settings":
      return <SettingsPage />;
    default:
      return <AiTradingPage />;
  }
}

export default function App() {
  return (
    <Layout>
      {(activeTab) => (
        <Suspense
          fallback={(
            <div className="flex min-h-64 items-center justify-center text-slate-500">
              页面加载中…
            </div>
          )}
        >
          {renderPage(activeTab)}
        </Suspense>
      )}
    </Layout>
  );
}
