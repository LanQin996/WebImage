"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { LoaderCircle, Plus, Power, PowerOff, RefreshCw, Trash2, PlugZap, X } from "lucide-react";
import { toast } from "sonner";

import { PageEnter } from "@/components/page-enter";
import { PageLoader } from "@/components/page-loader";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  batchProxyPoolEntries,
  deleteProxyPoolEntry,
  fetchProxyPool,
  fetchProxySubscriptions,
  testProxyPoolEntry,
  updateProxyPoolEntry,
  updateProxyPoolSettings,
  type ProxyCoreStatus,
  type ProxyPoolItem,
  type ProxyPoolPayload,
  type ProxyPoolSettings,
  type ProxyPoolBatchAction,
  type ProxySubscriptionsPayload,
} from "@/lib/api";
import { pickProxyPoolPayload, proxyPoolLabel } from "@/lib/proxy-pool";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { cn } from "@/lib/utils";

import { ProxyAddDialog } from "./components/proxy-add-dialog";
import { ProxyBulkDeleteDialog, ProxyDeleteDialog, ProxyPoolToggleDialog } from "./components/proxy-confirm-dialogs";
import { ProxyEditDialog } from "./components/proxy-edit-dialog";
import { ProxyPoolOverviewCard } from "./components/proxy-pool-overview-card";
import { ProxyPoolTable } from "./components/proxy-pool-table";
import { ProxyProbeSettingsCard } from "./components/proxy-probe-settings-card";
import { ProxySubscriptionsPanel } from "./components/proxy-subscriptions-panel";

const POLL_INTERVAL = 10_000;

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function addToSet(set: Set<string>, id: string, add: boolean) {
  const next = new Set(set);
  if (add) {
    next.add(id);
  } else {
    next.delete(id);
  }
  return next;
}

function ProxiesPageContent() {
  const [pool, setPool] = useState<ProxyPoolPayload | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [isTogglingPool, setIsTogglingPool] = useState(false);
  const [isSavingProbe, setIsSavingProbe] = useState(false);
  const [toggleTarget, setToggleTarget] = useState<boolean | null>(null);
  const [isAddOpen, setIsAddOpen] = useState(false);
  const [editingItem, setEditingItem] = useState<ProxyPoolItem | null>(null);
  const [deletingItem, setDeletingItem] = useState<ProxyPoolItem | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [pendingIds, setPendingIds] = useState<Set<string>>(() => new Set());
  const [testingIds, setTestingIds] = useState<Set<string>>(() => new Set());
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [bulkAction, setBulkAction] = useState<ProxyPoolBatchAction | null>(null);
  const [bulkDeleteIds, setBulkDeleteIds] = useState<string[] | null>(null);
  // 每次拉取或写操作都会递增；轮询结果回来时如果已经有更新的数据，就丢弃这次结果
  const versionRef = useRef(0);
  const [tab, setTab] = useState("pool");
  const [subscriptions, setSubscriptions] = useState<ProxySubscriptionsPayload | null>(null);
  const [isLoadingSubscriptions, setIsLoadingSubscriptions] = useState(true);
  const [subscriptionsError, setSubscriptionsError] = useState("");
  const subscriptionsVersionRef = useRef(0);

  const applyPayload = useCallback((data: ProxyPoolPayload) => {
    versionRef.current += 1;
    setPool(pickProxyPoolPayload(data));
    const validIds = new Set(data.items.map((item) => item.id));
    setSelectedIds((current) => new Set([...current].filter((id) => validIds.has(id))));
    setBulkDeleteIds((current) => {
      const remaining = current?.filter((id) => validIds.has(id));
      return remaining?.length ? remaining : null;
    });
    setLoadError("");
  }, []);

  const loadPool = useCallback(async (silent = false) => {
    const version = ++versionRef.current;
    if (!silent) {
      setIsLoading(true);
    }
    try {
      const data = await fetchProxyPool();
      if (version !== versionRef.current) {
        return;
      }
      applyPayload(data);
    } catch (error) {
      if (version !== versionRef.current) {
        return;
      }
      const message = errorMessage(error, "加载代理池失败");
      setLoadError(message);
      if (!silent) {
        toast.error(message);
      }
    } finally {
      if (!silent) {
        setIsLoading(false);
      }
    }
  }, [applyPayload]);

  // 订阅列表的错误不弹 toast（例如后端还没有这个接口），只在订阅页里显示
  const loadSubscriptions = useCallback(async () => {
    const version = ++subscriptionsVersionRef.current;
    try {
      const data = await fetchProxySubscriptions();
      if (version !== subscriptionsVersionRef.current) {
        return;
      }
      setSubscriptions({ items: data.items, core: data.core });
      setSubscriptionsError("");
    } catch (error) {
      if (version !== subscriptionsVersionRef.current) {
        return;
      }
      setSubscriptionsError(errorMessage(error, "加载订阅失败"));
    } finally {
      if (version === subscriptionsVersionRef.current) {
        setIsLoadingSubscriptions(false);
      }
    }
  }, []);

  const applySubscriptions = useCallback(
    (data: ProxySubscriptionsPayload) => {
      subscriptionsVersionRef.current += 1;
      setSubscriptions({ items: data.items, core: data.core });
      setSubscriptionsError("");
      setIsLoadingSubscriptions(false);
      // 导入 / 删除 / 刷新订阅都会改动池里的条目
      void loadPool(true);
    },
    [loadPool],
  );

  const applyCoreStatus = useCallback(
    (core: ProxyCoreStatus) => {
      subscriptionsVersionRef.current += 1;
      setSubscriptions((current) => (current ? { ...current, core } : current));
      // 内核开关会让内核节点恢复 / 禁用，节点能否导入也跟着变
      void loadSubscriptions();
      void loadPool(true);
    },
    [loadPool, loadSubscriptions],
  );

  // 页面可见时每 10 秒刷新一次健康状态和绑定数；切到后台就停
  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    const stop = () => {
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
    };
    const start = (silent: boolean) => {
      stop();
      void loadPool(silent);
      void loadSubscriptions();
      timer = setInterval(() => {
        void loadPool(true);
        void loadSubscriptions();
      }, POLL_INTERVAL);
    };
    const handleVisibility = () => (document.visibilityState === "visible" ? start(true) : stop());

    start(false);
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [loadPool, loadSubscriptions]);

  const settings = pool?.settings ?? null;
  const items = pool?.items ?? [];
  const poolEnabled = Boolean(settings?.enabled);
  const selectedItems = items.filter((item) => selectedIds.has(item.id));
  const bulkDeleteItems = items.filter((item) => bulkDeleteIds?.includes(item.id));
  const bulkBusy = bulkAction !== null;
  const bulkUnavailable = bulkBusy || pendingIds.size > 0 || testingIds.size > 0 || isDeleting;

  const handleSelect = (id: string, selected: boolean) => {
    setSelectedIds((current) => addToSet(current, id, selected));
  };

  const handleSelectAll = (selected: boolean) => {
    setSelectedIds(selected ? new Set(items.map((item) => item.id)) : new Set());
  };

  const handleBulkAction = async (action: ProxyPoolBatchAction, ids: string[]) => {
    if (ids.length === 0 || bulkUnavailable) return;
    setBulkAction(action);
    try {
      const data = await batchProxyPoolEntries(ids, action);
      applyPayload(data);
      if (action === "test") {
        const available = data.results?.filter((entry) => entry.result.ok).length ?? 0;
        toast[available === ids.length ? "success" : "warning"](`检测完成：${available}/${ids.length} 个代理可用`);
      } else if (action === "delete") {
        setSelectedIds(new Set());
        setBulkDeleteIds(null);
        const bindingMessage = data.reassigned
          ? items.length === ids.length
            ? `，解除 ${data.reassigned} 个账号的绑定`
            : `，处理 ${data.reassigned} 个账号的绑定`
          : "";
        toast.success(`已删除 ${data.affected} 个代理${bindingMessage}`);
        if (bulkDeleteItems.some((item) => item.subscription_id)) void loadSubscriptions();
      } else {
        setSelectedIds(new Set());
        toast.success(`已${action === "enable" ? "启用" : "禁用"} ${data.affected} 个代理`);
      }
    } catch (error) {
      toast.error(errorMessage(error, "批量操作失败"));
    } finally {
      setBulkAction(null);
    }
  };

  const handleToggleConfirm = async (next: boolean) => {
    setIsTogglingPool(true);
    try {
      const data = await updateProxyPoolSettings({ enabled: next });
      applyPayload(data);
      setToggleTarget(null);
      if (next) {
        const parts = ["代理池已开启"];
        if (data.migration?.migrated) {
          parts.push(
            `迁移 ${data.migration.migrated} 个账号的原有代理${data.migration.created ? `（新增 ${data.migration.created} 个代理）` : ""}`,
          );
        }
        if (data.assigned) {
          parts.push(`为 ${data.assigned} 个账号分配了代理`);
        }
        toast.success(parts.join("，"));
      } else {
        toast.success("代理池已关闭");
      }
    } catch (error) {
      toast.error(errorMessage(error, "更新代理池开关失败"));
    } finally {
      setIsTogglingPool(false);
    }
  };

  const handleSaveProbe = async (updates: Partial<ProxyPoolSettings>) => {
    setIsSavingProbe(true);
    try {
      const data = await updateProxyPoolSettings(updates);
      applyPayload(data);
      toast.success("探测参数已保存");
      return true;
    } catch (error) {
      toast.error(errorMessage(error, "保存探测参数失败"));
      return false;
    } finally {
      setIsSavingProbe(false);
    }
  };

  const handleToggleItem = async (item: ProxyPoolItem, enabled: boolean) => {
    setPendingIds((current) => addToSet(current, item.id, true));
    try {
      const data = await updateProxyPoolEntry(item.id, { enabled });
      applyPayload(data);
      toast.success(`${proxyPoolLabel(item)} 已${enabled ? "启用" : "禁用"}`);
    } catch (error) {
      toast.error(errorMessage(error, "更新代理失败"));
    } finally {
      setPendingIds((current) => addToSet(current, item.id, false));
    }
  };

  const handleTestItem = async (item: ProxyPoolItem) => {
    setTestingIds((current) => addToSet(current, item.id, true));
    try {
      const data = await testProxyPoolEntry(item.id);
      applyPayload(data);
      const result = data.result;
      if (result?.ok) {
        toast.success(
          `${proxyPoolLabel(item)} 可用：${result.latency_ms} ms${result.exit_ip ? `，出口 ${result.exit_ip}` : ""}`,
        );
      } else {
        toast.error(`${proxyPoolLabel(item)} 不可用：${result?.error || `HTTP ${result?.status ?? "-"}`}`);
      }
    } catch (error) {
      toast.error(errorMessage(error, "测试代理失败"));
    } finally {
      setTestingIds((current) => addToSet(current, item.id, false));
    }
  };

  const handleDelete = async () => {
    if (!deletingItem) {
      return;
    }
    const item = deletingItem;
    setIsDeleting(true);
    setPendingIds((current) => addToSet(current, item.id, true));
    try {
      const data = await deleteProxyPoolEntry(item.id);
      applyPayload(data);
      setDeletingItem(null);
      toast.success(data.reassigned ? `代理已删除，${data.reassigned} 个账号已改绑` : "代理已删除");
      if (item.subscription_id) {
        // 订阅的已导入数跟着变
        void loadSubscriptions();
      }
    } catch (error) {
      toast.error(errorMessage(error, "删除代理失败"));
    } finally {
      setIsDeleting(false);
      setPendingIds((current) => addToSet(current, item.id, false));
    }
  };

  // 删除确认里的绑定数跟着轮询更新
  const deletingLive = deletingItem ? (items.find((item) => item.id === deletingItem.id) ?? deletingItem) : null;

  const subscriptionItems = subscriptions?.items ?? [];
  const core = subscriptions?.core ?? null;
  // 订阅拉取失败、有节点不可用或内核连不上时，在标签上提示一下
  const subscriptionsNeedAttention =
    subscriptionItems.some((item) => item.last_error || item.missing_count > 0) ||
    Boolean(core?.settings.enabled && !core.status.ok);

  return (
    <PageEnter className="space-y-3">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 flex-wrap items-center gap-x-3.5 gap-y-1">
          <h1 className="text-xl leading-tight font-semibold tracking-tight text-foreground">代理池</h1>
          <span className="text-xs text-muted-foreground">每个账号粘性绑定一个上游代理，每 {POLL_INTERVAL / 1000} 秒自动刷新</span>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            variant="outline"
            size="icon-sm"
            aria-label="刷新"
            title="立即刷新"
            onClick={() => void loadPool()}
            disabled={isLoading}
          >
            <RefreshCw className={cn("size-4", isLoading ? "animate-spin" : "")} />
          </Button>
          <Button size="sm" onClick={() => setIsAddOpen(true)} disabled={!pool}>
            <Plus className="size-4" />
            批量添加
          </Button>
        </div>
      </header>

      {loadError && pool ? (
        <div className="rounded-xl bg-destructive/10 px-3.5 py-2 text-xs text-destructive">刷新失败：{loadError}，显示的是上一次的数据。</div>
      ) : null}

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(0,400px)]">
        <ProxyPoolOverviewCard
          enabled={poolEnabled}
          summary={pool?.summary ?? null}
          isSaving={isTogglingPool}
          disabled={!pool}
          onToggle={(next) => setToggleTarget(next)}
        />
        <ProxyProbeSettingsCard settings={settings} isSaving={isSavingProbe} onSave={handleSaveProbe} />
      </div>

      <Tabs value={tab} onValueChange={setTab} className="gap-3">
        <TabsList aria-label="代理池视图">
          <TabsTrigger value="pool">
            代理
            <span className="text-xs tabular-nums opacity-70">{items.length}</span>
          </TabsTrigger>
          <TabsTrigger value="subscriptions">
            订阅
            <span className="text-xs tabular-nums opacity-70">{subscriptionItems.length}</span>
            {subscriptionsNeedAttention ? (
              <span className="size-1.5 rounded-full bg-warning" role="img" aria-label="有订阅或内核需要处理" />
            ) : null}
          </TabsTrigger>
        </TabsList>

        <TabsContent value="pool">
          <Card className="overflow-hidden">
            <CardContent className="p-0">
              {!pool && !isLoading && loadError ? (
                <div className="flex flex-col items-center gap-3 px-6 py-14 text-center">
                  <p className="text-sm text-destructive">{loadError}</p>
                  <Button variant="outline" size="sm" onClick={() => void loadPool()}>
                    <RefreshCw className="size-4" />
                    重试
                  </Button>
                </div>
              ) : (
                <ProxyPoolTable
                  items={items}
                  isLoading={isLoading && !pool}
                  pendingIds={pendingIds}
                  testingIds={testingIds}
                  selectedIds={selectedIds}
                  selectionDisabled={bulkBusy}
                  onSelect={handleSelect}
                  onSelectAll={handleSelectAll}
                  onToggle={(item, enabled) => void handleToggleItem(item, enabled)}
                  onTest={(item) => void handleTestItem(item)}
                  onEdit={setEditingItem}
                  onDelete={setDeletingItem}
                  onAdd={() => setIsAddOpen(true)}
                />
              )}
              {items.length > 0 ? (
                <div className="border-t border-border px-3.5 py-2 text-xs text-muted-foreground tabular-nums">共 {items.length} 个代理</div>
              ) : null}
            </CardContent>
          </Card>
          {selectedItems.length > 0 ? (
            <div role="toolbar" aria-label="代理批量操作" className="sticky bottom-3 z-20 mt-3 flex max-w-full flex-wrap items-center gap-1.5 rounded-md border border-border bg-background p-2 shadow-lg">
              <span className="px-2 text-xs tabular-nums text-muted-foreground">已选 {selectedItems.length} 项</span>
              <Button variant="outline" size="sm" disabled={bulkUnavailable || selectedItems.every((item) => item.enabled)} onClick={() => void handleBulkAction("enable", selectedItems.map((item) => item.id))}>
                {bulkAction === "enable" ? <LoaderCircle className="size-4 animate-spin" /> : <Power className="size-4" />}
                启用
              </Button>
              <Button variant="outline" size="sm" disabled={bulkUnavailable || selectedItems.every((item) => !item.enabled)} onClick={() => void handleBulkAction("disable", selectedItems.map((item) => item.id))}>
                {bulkAction === "disable" ? <LoaderCircle className="size-4 animate-spin" /> : <PowerOff className="size-4" />}
                禁用
              </Button>
              <Button variant="outline" size="sm" disabled={bulkUnavailable} onClick={() => void handleBulkAction("test", selectedItems.map((item) => item.id))}>
                {bulkAction === "test" ? <LoaderCircle className="size-4 animate-spin" /> : <PlugZap className="size-4" />}
                检测
              </Button>
              <Button variant="outline" size="sm" className="text-destructive hover:text-destructive" disabled={bulkUnavailable} onClick={() => setBulkDeleteIds(selectedItems.map((item) => item.id))}>
                <Trash2 className="size-4" />
                删除
              </Button>
              <Button variant="ghost" size="icon-sm" aria-label="取消选择" title="取消选择" disabled={bulkBusy} onClick={() => setSelectedIds(new Set())}>
                <X className="size-4" />
              </Button>
            </div>
          ) : null}
        </TabsContent>

        <TabsContent value="subscriptions">
          <ProxySubscriptionsPanel
            payload={subscriptions}
            isLoading={isLoadingSubscriptions}
            loadError={subscriptionsError}
            onReload={() => void loadSubscriptions()}
            onApply={applySubscriptions}
            onCoreSaved={applyCoreStatus}
          />
        </TabsContent>
      </Tabs>

      <ProxyPoolToggleDialog
        target={toggleTarget}
        summary={pool?.summary ?? null}
        isSaving={isTogglingPool}
        onCancel={() => setToggleTarget(null)}
        onConfirm={(next) => void handleToggleConfirm(next)}
      />
      <ProxyAddDialog
        open={isAddOpen}
        onOpenChange={setIsAddOpen}
        poolEnabled={poolEnabled}
        onAdded={(data) => {
          applyPayload(data);
          // 粘贴的分享链接会存进「手动添加的节点」订阅
          void loadSubscriptions();
        }}
      />
      <ProxyEditDialog item={editingItem} onClose={() => setEditingItem(null)} onSaved={applyPayload} />
      <ProxyDeleteDialog
        item={deletingLive}
        hasOtherProxies={items.some((item) => item.id !== deletingLive?.id)}
        isDeleting={isDeleting}
        onCancel={() => setDeletingItem(null)}
        onConfirm={() => void handleDelete()}
      />
      <ProxyBulkDeleteDialog
        count={bulkDeleteItems.length}
        boundCount={bulkDeleteItems.reduce((total, item) => total + item.bound_count, 0)}
        remainingCount={items.length - bulkDeleteItems.length}
        subscriptionCount={bulkDeleteItems.filter((item) => item.subscription_id).length}
        isDeleting={bulkAction === "delete"}
        onCancel={() => setBulkDeleteIds(null)}
        onConfirm={() => void handleBulkAction("delete", bulkDeleteItems.map((item) => item.id))}
      />
    </PageEnter>
  );
}

export default function ProxiesPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);

  if (isCheckingAuth || !session || session.role !== "admin") {
    return <PageLoader />;
  }

  return <ProxiesPageContent />;
}
