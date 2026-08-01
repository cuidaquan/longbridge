import { useEffect, useMemo, useState } from "react";
import {
  ArrowDownward,
  ArrowUpward,
  Close,
  DeleteOutline,
  PushPin,
  Refresh,
  Search,
  Star,
  UnfoldMore,
} from "@mui/icons-material";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  LoadingSpinner,
  PageHeader,
} from "../components/ui";
import {
  fetchWatchlistQuotes,
  fetchWatchlists,
  removeWatchlistSecurity,
  updateWatchlistPinned,
  WatchlistGroup,
  WatchlistQuote,
} from "../api/client";

function displayGroupName(name: string): string {
  const labels: Record<string, string> = {
    all: "全部",
    us: "美股",
    hk: "港股",
    sg: "新加坡",
  };
  return labels[name.toLowerCase()] || name;
}

function formatPrice(value: number | null): string {
  return value == null ? "—" : value.toFixed(3);
}

function formatChange(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function quoteTone(value: number | null): string {
  if (value == null || value === 0) {
    return "bg-slate-100 text-slate-500 dark:bg-slate-700/70 dark:text-slate-300";
  }
  return value > 0
    ? "bg-red-500 text-white"
    : "bg-emerald-500 text-white";
}

type ChangeSort = "none" | "desc" | "asc";

export default function WatchlistPage() {
  const [groups, setGroups] = useState<WatchlistGroup[]>([]);
  const [quotes, setQuotes] = useState<Record<string, WatchlistQuote>>({});
  const [loading, setLoading] = useState(true);
  const [quoteLoading, setQuoteLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [selectedGroupId, setSelectedGroupId] = useState<number | null>(null);
  const [query, setQuery] = useState("");
  const [pinnedOnly, setPinnedOnly] = useState(false);
  const [changeSort, setChangeSort] = useState<ChangeSort>("none");
  const [removingSymbol, setRemovingSymbol] = useState<string | null>(null);
  const [updatingPinSymbol, setUpdatingPinSymbol] = useState<string | null>(null);

  const loadWatchlists = async () => {
    try {
      setLoading(true);
      const response = await fetchWatchlists();
      const nextGroups = response.groups || [];
      setGroups(nextGroups);
      setLastUpdated(new Date());
      setError(null);

      const symbols = [...new Set(
        nextGroups.flatMap((group) => group.securities.map((security) => security.symbol)),
      )];
      if (symbols.length > 0) {
        try {
          setQuoteLoading(true);
          const quoteResponse = await fetchWatchlistQuotes(symbols);
          setQuotes(quoteResponse.quotes || {});
        } catch (reason: any) {
          setQuotes({});
          setError(reason?.message || "行情暂时不可用，列表仍可查看");
        } finally {
          setQuoteLoading(false);
        }
      } else {
        setQuotes({});
      }
    } catch (reason: any) {
      setError(reason?.message || "无法获取 Longbridge 关注列表");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadWatchlists();
    const interval = setInterval(loadWatchlists, 60000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    if (groups.length === 0) {
      setSelectedGroupId(null);
      return;
    }
    if (selectedGroupId !== null && groups.some((group) => group.id === selectedGroupId)) {
      return;
    }
    const preferred = groups.find((group) => group.name.toLowerCase() === "us") || groups[0];
    setSelectedGroupId(preferred.id);
  }, [groups, selectedGroupId]);

  const selectedGroup = groups.find((group) => group.id === selectedGroupId) || null;
  const visibleSecurities = useMemo(() => {
    if (!selectedGroup) return [];
    const normalizedQuery = query.trim().toLowerCase();
    return selectedGroup.securities
      .filter((security) => {
        if (pinnedOnly && !security.is_pinned) return false;
        const searchableName = `${security.name_cn || ""} ${security.name_en || ""} ${security.name}`;
        return !normalizedQuery
          || `${security.symbol} ${searchableName}`.toLowerCase().includes(normalizedQuery);
      })
      .sort((left, right) => {
        if (changeSort !== "none") {
          const leftChange = quotes[left.symbol.toUpperCase()]?.change_rate ?? null;
          const rightChange = quotes[right.symbol.toUpperCase()]?.change_rate ?? null;
          if (leftChange == null && rightChange != null) return 1;
          if (leftChange != null && rightChange == null) return -1;
          if (leftChange != null && rightChange != null && leftChange !== rightChange) {
            return changeSort === "desc"
              ? rightChange - leftChange
              : leftChange - rightChange;
          }
        }
        return Number(right.is_pinned) - Number(left.is_pinned);
      });
  }, [changeSort, pinnedOnly, query, quotes, selectedGroup]);

  const cycleChangeSort = () => {
    setChangeSort((current) => (
      current === "none" ? "desc" : current === "desc" ? "asc" : "none"
    ));
  };

  const cancelWatch = async (symbol: string, name: string) => {
    if (!window.confirm(`确认取消关注“${name}”（${symbol}）？\n\n该标的会从 Longbridge 的所有关注分组中移除。`)) {
      return;
    }
    try {
      setRemovingSymbol(symbol);
      await removeWatchlistSecurity(symbol);
      await loadWatchlists();
    } catch (reason: any) {
      setError(reason?.message || `取消关注 ${symbol} 失败`);
    } finally {
      setRemovingSymbol(null);
    }
  };

  const togglePin = async (symbol: string, isPinned: boolean) => {
    try {
      setUpdatingPinSymbol(symbol);
      await updateWatchlistPinned(symbol, !isPinned);
      await loadWatchlists();
    } catch (reason: any) {
      setError(reason?.message || `${isPinned ? "取消置顶" : "置顶"} ${symbol} 失败`);
    } finally {
      setUpdatingPinSymbol(null);
    }
  };

  if (loading && groups.length === 0) {
    return <LoadingSpinner size="lg" text="加载关注列表..." />;
  }

  return (
    <div className="space-y-5 animate-fade-in">
      <PageHeader
        title="关注"
        description="自选标的与实时行情"
        icon={<Star />}
        actions={(
          <div className="flex items-center gap-3">
            <span className="hidden text-sm text-slate-500 dark:text-slate-400 sm:inline">
              {quoteLoading ? "行情更新中" : lastUpdated ? lastUpdated.toLocaleTimeString() : "未更新"}
            </span>
            <Button
              variant="secondary"
              onClick={loadWatchlists}
              loading={loading || quoteLoading}
              icon={<Refresh className="h-4 w-4" />}
            >
              刷新
            </Button>
          </div>
        )}
      />

      {error && <Alert type="error">{error}</Alert>}

      {groups.length === 0 ? (
        <EmptyState
          title="暂无关注分组"
          description="请先在 Longbridge 中添加自选标的"
          icon={<Star />}
        />
      ) : (
        <div className="space-y-4">
          <Card padding="sm">
            <div className="flex flex-col gap-3">
              <div className="flex gap-2 overflow-x-auto pb-1" role="tablist" aria-label="关注分组">
                {groups.map((group) => {
                  const isActive = group.id === selectedGroupId;
                  return (
                    <button
                      key={group.id}
                      type="button"
                      role="tab"
                      aria-selected={isActive}
                      onClick={() => setSelectedGroupId(group.id)}
                      className={`whitespace-nowrap rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                        isActive
                          ? "bg-cyan-500 text-white shadow-sm"
                          : "bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-700 dark:text-slate-300 dark:hover:bg-slate-600"
                      }`}
                    >
                      {displayGroupName(group.name)}
                      <span className="ml-1.5 opacity-75">{group.securities.length}</span>
                    </button>
                  );
                })}
              </div>
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="relative min-w-0 flex-1 sm:max-w-md">
                  <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
                  <input
                    type="search"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="搜索名称或代码"
                    aria-label="搜索关注标的"
                    className="w-full rounded-lg border border-slate-200 bg-slate-50 py-2 pl-9 pr-9 text-sm text-slate-900 outline-none transition focus:border-cyan-500 focus:ring-2 focus:ring-cyan-500/20 dark:border-slate-700 dark:bg-slate-900 dark:text-white"
                  />
                  {query && (
                    <button
                      type="button"
                      aria-label="清除搜索"
                      onClick={() => setQuery("")}
                      className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-slate-400 hover:bg-slate-200 hover:text-slate-600 dark:hover:bg-slate-700 dark:hover:text-slate-200"
                    >
                      <Close className="h-4 w-4" />
                    </button>
                  )}
                </div>
                <Button
                  variant={pinnedOnly ? "primary" : "secondary"}
                  size="sm"
                  onClick={() => setPinnedOnly((value) => !value)}
                  icon={<Star className="h-4 w-4" />}
                  aria-pressed={pinnedOnly}
                >
                  仅看置顶
                </Button>
              </div>
            </div>
          </Card>

          {selectedGroup && (
            <Card padding="none">
              <div className="p-4 md:p-5">
                <CardHeader
                  title={displayGroupName(selectedGroup.name)}
                  description={`显示 ${visibleSecurities.length} / ${selectedGroup.securities.length} 个标的`}
                  icon={<Star className="h-5 w-5" />}
                  action={<Badge variant="info">{visibleSecurities.length} 个标的</Badge>}
                />
                {visibleSecurities.length === 0 ? (
                  <div className="py-8">
                    <EmptyState
                      title={query || pinnedOnly ? "没有匹配的标的" : "此分组暂无标的"}
                      description={query || pinnedOnly ? "可以清除搜索或关闭置顶筛选" : "请先在 Longbridge 中添加自选标的"}
                      icon={<Search />}
                    />
                  </div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[720px] text-sm">
                      <thead>
                        <tr className="border-b border-slate-200 text-left dark:border-slate-700">
                          <th className="px-3 py-3 font-medium text-slate-500 dark:text-slate-400">名称 / 代码</th>
                          <th className="px-3 py-3 text-right font-medium text-slate-500 dark:text-slate-400">最新价</th>
                          <th className="px-3 py-3 text-right font-medium text-slate-500 dark:text-slate-400">
                            <button
                              type="button"
                              onClick={cycleChangeSort}
                              aria-label={changeSort === "desc" ? "涨跌幅从高到低，点击切换" : changeSort === "asc" ? "涨跌幅从低到高，点击恢复默认" : "按涨跌幅排序"}
                              className="inline-flex items-center gap-1 rounded px-1 py-0.5 transition-colors hover:bg-slate-100 hover:text-slate-900 dark:hover:bg-slate-700 dark:hover:text-white"
                            >
                              涨跌幅
                              {changeSort === "desc" ? <ArrowDownward className="h-3.5 w-3.5" /> : changeSort === "asc" ? <ArrowUpward className="h-3.5 w-3.5" /> : <UnfoldMore className="h-3.5 w-3.5 opacity-60" />}
                            </button>
                          </th>
                          <th className="px-3 py-3 text-right font-medium text-slate-500 dark:text-slate-400">操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {visibleSecurities.map((security) => {
                          const quote = quotes[security.symbol.toUpperCase()];
                          const displayName = security.name_cn || security.name || security.symbol;
                          return (
                            <tr
                              key={`${selectedGroup.id}-${security.symbol}`}
                              className="border-b border-slate-100 last:border-0 hover:bg-slate-50 dark:border-slate-700/50 dark:hover:bg-slate-800/50"
                            >
                              <td className="px-3 py-3">
                                <div className="flex items-center gap-2">
                                  {security.is_pinned && <PushPin className="h-3.5 w-3.5 shrink-0 text-amber-500" />}
                                  <div className="min-w-0">
                                    <div className="truncate font-semibold text-slate-900 dark:text-white">{displayName}</div>
                                    <div className="text-xs font-medium text-slate-500 dark:text-slate-400">{security.symbol}</div>
                                  </div>
                                </div>
                              </td>
                              <td className="px-3 py-3 text-right font-semibold tabular-nums text-slate-900 dark:text-white">
                                {formatPrice(quote?.last_done ?? null)}
                              </td>
                              <td className="px-3 py-3 text-right">
                                <span className={`inline-flex min-w-[84px] justify-center rounded-md px-2.5 py-1 font-semibold tabular-nums ${quoteTone(quote?.change_rate ?? null)}`}>
                                  {formatChange(quote?.change_rate ?? null)}
                                </span>
                              </td>
                              <td className="px-3 py-3 text-right">
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  icon={<PushPin className="h-4 w-4" />}
                                  loading={updatingPinSymbol === security.symbol}
                                  onClick={() => togglePin(security.symbol, security.is_pinned)}
                                  title={security.is_pinned ? "取消置顶" : "置顶"}
                                  className={security.is_pinned
                                    ? "text-red-600 hover:text-red-700 dark:text-red-400 dark:hover:text-red-300"
                                    : "text-amber-600 hover:text-amber-700 dark:text-amber-400 dark:hover:text-amber-300"}
                                >
                                  {security.is_pinned ? "取消置顶" : "置顶"}
                                </Button>
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  icon={<DeleteOutline className="h-4 w-4" />}
                                  loading={removingSymbol === security.symbol}
                                  onClick={() => cancelWatch(security.symbol, displayName)}
                                  title="从所有关注分组中移除"
                                  className="text-red-600 hover:text-red-700 dark:text-red-400 dark:hover:text-red-300"
                                >
                                  取消关注
                                </Button>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}
