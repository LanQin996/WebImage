"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Bot,
  CheckCircle2,
  LoaderCircle,
  Mail,
  Pause,
  Play,
  RefreshCw,
  Save,
  ShieldCheck,
  Square,
  Terminal,
  Trash2,
  Waves,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  fetchKeeperStatus,
  fetchLatestRegisterJob,
  fetchRegisterProgress,
  fetchRegisterSettings,
  startRegister,
  stopRegister,
  subscribeRegisterLogs,
  testRegisterMail,
  testRegisterProxy,
  updateRegisterSettings,
  type KeeperStatus,
  type ProxyProbeResult,
  type RegisterJobSnapshot,
  type RegisterLogEntry,
  type RegisterSettings,
} from "@/lib/api";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { cn } from "@/lib/utils";

const levelStyle: Record<string, string> = {
  info: "text-stone-500",
  step: "text-sky-600",
  success: "text-emerald-600",
  warn: "text-amber-600",
  error: "text-rose-600",
};

const levelIcon: Record<string, string> = {
  info: "·",
  step: "→",
  success: "✓",
  warn: "!",
  error: "✗",
};

const REGISTER_JOB_STORAGE_KEY = "chatgpt2api-register-job-id";

function formatTime(ts: number) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}

export default function RegisterPage() {
  const { isCheckingAuth } = useAuthGuard(["admin"]);

  const [settings, setSettings] = useState<RegisterSettings | null>(null);
  const [keeper, setKeeper] = useState<KeeperStatus | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isTesting, setIsTesting] = useState(false);
  const [isTestingProxy, setIsTestingProxy] = useState(false);
  const [proxyProbe, setProxyProbe] = useState<{
    parsed: number;
    tested: number;
    available: number;
    results: ProxyProbeResult[];
  } | null>(null);

  const [count, setCount] = useState("1");
  const [concurrency, setConcurrency] = useState("1");
  const [password, setPassword] = useState("");

  const [job, setJob] = useState<RegisterJobSnapshot | null>(null);
  const [logs, setLogs] = useState<RegisterLogEntry[]>([]);
  const [streamError, setStreamError] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);

  const jobIdRef = useRef<string>("");
  const jobRef = useRef<RegisterJobSnapshot | null>(null);
  const unsubscribeRef = useRef<(() => void) | null>(null);
  const selectionVersionRef = useRef(0);
  const streamVersionRef = useRef(0);
  const logBoxRef = useRef<HTMLDivElement | null>(null);

  const running = Boolean(job && !job.finished);
  const manualRunning = Boolean(running && job?.source === "manual");

  const setCurrentJob = (snapshot: RegisterJobSnapshot) => {
    jobRef.current = snapshot;
    setJob(snapshot);
  };

  const terminateStream = () => {
    streamVersionRef.current += 1;
    unsubscribeRef.current?.();
    unsubscribeRef.current = null;
  };

  useEffect(() => () => terminateStream(), []);

  const loadSettings = async (silent = false) => {
    if (!silent) setIsLoading(true);
    try {
      const data = await fetchRegisterSettings();
      setSettings(data.settings);
      if (!silent) {
        setCount(String(data.settings.default_count || 1));
        setConcurrency(String(data.settings.default_concurrency || 1));
        setPassword(data.settings.fixed_password || "");
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载注册配置失败");
    } finally {
      setIsLoading(false);
    }
  };

  const loadKeeper = async () => {
    try {
      const data = await fetchKeeperStatus();
      setKeeper(data.keeper);
    } catch {
      // 状态拿不到不打扰
    }
  };

  useEffect(() => {
    void loadSettings();
    void loadKeeper();
    const id = setInterval(() => void loadKeeper(), 8000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (!autoScroll || !logBoxRef.current) return;
    logBoxRef.current.scrollTop = logBoxRef.current.scrollHeight;
  }, [logs, autoScroll]);

  const patch = (key: keyof RegisterSettings, value: unknown) => {
    setSettings((prev) => (prev ? ({ ...prev, [key]: value } as RegisterSettings) : prev));
  };

  const handleKeeperToggle = async (enabled: boolean) => {
    if (!settings || isSaving) return;
    const previous = settings.keeper_enabled;
    patch("keeper_enabled", enabled);
    setIsSaving(true);
    try {
      const data = await updateRegisterSettings({ keeper_enabled: enabled });
      patch("keeper_enabled", data.settings.keeper_enabled);
      await loadKeeper();
    } catch (error) {
      patch("keeper_enabled", previous);
      toast.error(error instanceof Error ? error.message : "更新守护状态失败");
    } finally {
      setIsSaving(false);
    }
  };

  const handleSave = async () => {
    if (!settings) return;
    setIsSaving(true);
    try {
      const data = await updateRegisterSettings({
        enabled: settings.enabled,
        mail_api_base: settings.mail_api_base,
        mail_auth_mode: settings.mail_auth_mode,
        mail_auth_key: settings.mail_auth_key,
        mail_domain: settings.mail_domain,
        proxy_pool_text: settings.proxy_pool_text,
        otp_max_wait: settings.otp_max_wait,
        otp_poll_interval: settings.otp_poll_interval,
        otp_total_budget: settings.otp_total_budget,
        token_source: settings.token_source,
        auto_locale_from_ip: settings.auto_locale_from_ip,
        anon_bootstrap: settings.anon_bootstrap,
        fixed_password: password,
        default_count: Number(count) || 1,
        default_concurrency: Number(concurrency) || 1,
        keeper_enabled: settings.keeper_enabled,
        keeper_target_accounts: settings.keeper_target_accounts,
        keeper_target_quota: settings.keeper_target_quota,
        keeper_min_quota: settings.keeper_min_quota,
        keeper_estimate_quota: settings.keeper_estimate_quota,
        keeper_check_interval: settings.keeper_check_interval,
        keeper_max_per_round: settings.keeper_max_per_round,
        keeper_concurrency: settings.keeper_concurrency,
        keeper_daily_limit: settings.keeper_daily_limit,
        keeper_source_type: settings.keeper_source_type,
        keeper_fail_threshold: settings.keeper_fail_threshold,
        keeper_fail_cooldown_minutes: settings.keeper_fail_cooldown_minutes,
      });
      setSettings(data.settings);
      void loadKeeper();
      toast.success("配置已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setIsSaving(false);
    }
  };

  const handleTestProxy = async () => {
    if (!settings) return;
    setIsTestingProxy(true);
    setProxyProbe(null);
    try {
      const data = await testRegisterProxy({
        proxy_pool_text: settings.proxy_pool_text,
        limit: 5,
      });
      setProxyProbe(data);
      if (data.tested === 0) {
        toast.error("没有可测试的代理线路，请检查格式");
      } else if (data.available === 0) {
        toast.error("所有测试线路都无法访问 chatgpt.com，换一批 IP 再试", { duration: 10000 });
      } else {
        toast.success(`${data.available}/${data.tested} 条线路可用`);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "代理测试失败");
    } finally {
      setIsTestingProxy(false);
    }
  };

  const handleTestMail = async () => {
    if (!settings) return;
    setIsTesting(true);
    try {
      const data = await testRegisterMail({
        mail_api_base: settings.mail_api_base,
        mail_auth_mode: settings.mail_auth_mode,
        mail_auth_key: settings.mail_auth_key,
        mail_domain: settings.mail_domain,
      });
      toast.success(`邮箱服务可用：${data.result.address}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "邮箱测试失败";
      // 403 常见于 Cloudflare 拦截出站 IP，而不是邮箱服务本身的问题
      toast.error(message.includes("403") ? `${message}（疑似 Cloudflare 拦截出口 IP）` : message, {
        duration: 8000,
      });
    } finally {
      setIsTesting(false);
    }
  };

  const attachStream = (jobId: string, after: number, notifyDone = false) => {
    terminateStream();
    const streamVersion = streamVersionRef.current;
    const isCurrentStream = () => streamVersionRef.current === streamVersion && jobIdRef.current === jobId;
    setStreamError("");
    unsubscribeRef.current = subscribeRegisterLogs(
      jobId,
      {
        onLog: (entry) => {
          if (isCurrentStream()) {
            setLogs((prev) => !isCurrentStream() || prev.some((x) => x.seq === entry.seq) ? prev : [...prev, entry]);
          }
        },
        onStatus: (snapshot) => {
          if (isCurrentStream()) setCurrentJob(snapshot);
        },
        onDone: (snapshot) => {
          if (!isCurrentStream()) return;
          setCurrentJob(snapshot);
          unsubscribeRef.current = null;
          if (notifyDone) toast.success(`任务结束：成功 ${snapshot.success} / 失败 ${snapshot.failed}`);
        },
        onError: (message) => {
          if (isCurrentStream()) setStreamError(message);
        },
      },
      after,
    );
  };

  const followJob = (snapshot: RegisterJobSnapshot, notifyDone = false) => {
    if (jobIdRef.current === snapshot.job_id) {
      if (!jobRef.current || snapshot.last_seq >= jobRef.current.last_seq) setCurrentJob(snapshot);
      return;
    }
    selectionVersionRef.current += 1;
    jobIdRef.current = snapshot.job_id;
    setCurrentJob(snapshot);
    setLogs([]);
    try {
      sessionStorage.setItem(REGISTER_JOB_STORAGE_KEY, snapshot.job_id);
    } catch {
      // 私密模式禁用存储时仍可跟随当前任务。
    }
    attachStream(snapshot.job_id, 0, notifyDone);
  };

  useEffect(() => {
    let cancelled = false;
    const restoreJob = async () => {
      const version = selectionVersionRef.current;
      let savedId = "";
      try {
        savedId = sessionStorage.getItem(REGISTER_JOB_STORAGE_KEY) || "";
      } catch {
        // 后端最近任务仍可用于恢复。
      }
      try {
        const latest = (await fetchLatestRegisterJob()).job;
        if (!cancelled && version === selectionVersionRef.current && latest) followJob(latest);
      } catch {
        if (!savedId) return;
        try {
          const saved = await fetchRegisterProgress(savedId);
          if (!cancelled && version === selectionVersionRef.current) followJob(saved);
        } catch {
          try { sessionStorage.removeItem(REGISTER_JOB_STORAGE_KEY); } catch { /* ignore */ }
        }
      }
    };
    void restoreJob();
    const timer = setInterval(async () => {
      const version = selectionVersionRef.current;
      try {
        const latest = (await fetchLatestRegisterJob()).job;
        if (cancelled || version !== selectionVersionRef.current || !latest) return;
        const current = jobRef.current;
        if (!current || (current.job_id !== latest.job_id && (current.finished || current.source !== "manual"))) {
          followJob(latest);
        }
      } catch {
        // 日志流自身会报告连接问题。
      }
    }, 8000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const handleStart = async () => {
    if (!settings?.ready) {
      toast.error("请先配置 cloudflare_temp_email 的 API 地址并保存");
      return;
    }
    if (keeper && !keeper.node.available) {
      toast.error("Node 运行环境未就绪，无法生成 sentinel token");
      return;
    }
    try {
      await updateRegisterSettings({ fixed_password: password });
    } catch {
      // 保存失败不阻塞启动
    }
    try {
      const data = await startRegister({
        count: Math.max(1, Number(count) || 1),
        concurrency: Math.max(1, Number(concurrency) || 1),
        password,
      });
      toast.success(`已启动注册任务（${data.total} 个）`);
      followJob({
        job_id: data.job_id, source: "manual", total: data.total, issued: 0, done: 0, success: 0, failed: 0,
        current_email: "", message: "已启动", finished: false, error: null, daily_blocked: false,
        results: [], last_seq: 0,
      }, true);
    } catch (error) {
      const message = error instanceof Error ? error.message : "启动失败";
      const hint = message.includes("403")
        ? "。403 通常是出口 IP 被 Cloudflare 拦截，请更换代理池中的节点"
        : "";
      toast.error(`${message}${hint}`, { duration: 10000 });
    }
  };

  const handleStop = async () => {
    if (!job) return;
    try {
      await stopRegister(job.job_id);
      toast.info("已请求停止");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "停止失败");
    }
  };

  const handleReconnect = async () => {
    const jobId = jobIdRef.current;
    if (!jobId) return;
    try {
      const snapshot = await fetchRegisterProgress(jobId);
      if (jobIdRef.current !== jobId) return;
      setCurrentJob(snapshot);
      attachStream(jobId, logs.length ? logs[logs.length - 1].seq : 0);
      toast.info("已重新连接日志流");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "重连失败");
    }
  };

  const progressPct = useMemo(() => {
    if (!job || job.total <= 0) return 0;
    return Math.min(100, (job.done / job.total) * 100);
  }, [job]);

  if (isCheckingAuth) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center text-stone-400">
        <LoaderCircle className="size-6 animate-spin" />
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-[1400px] space-y-5 px-4 py-6 sm:px-6">
      {/* 标题 + 操作 */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold tracking-tight text-stone-950">协议注册</h1>
          <p className="text-sm text-stone-500">
            对接自部署 cloudflare_temp_email，注册成功后获取 token 并导入号池。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            className="h-10 rounded-xl border-stone-200 bg-white text-stone-700"
            onClick={() => void loadSettings(true)}
            disabled={isLoading}
          >
            <RefreshCw className={cn("size-4", isLoading && "animate-spin")} />
            刷新
          </Button>
          <Button
            variant="outline"
            className="h-10 rounded-xl border-stone-200 bg-white text-stone-700"
            onClick={() => void handleSave()}
            disabled={isSaving || isLoading || !settings}
          >
            {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
            保存配置
          </Button>
          {manualRunning ? (
            <Button
              variant="outline"
              className="h-10 rounded-xl border-rose-200 bg-rose-50 text-rose-600 hover:bg-rose-100"
              onClick={() => void handleStop()}
            >
              <Square className="size-4" />
              停止
            </Button>
          ) : (
            <Button
              className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
              onClick={() => void handleStart()}
              disabled={isLoading || !settings?.ready}
            >
              <Play className="size-4" />
              开始注册
            </Button>
          )}
        </div>
      </div>

      {/* 环境告警 */}
      {keeper?.node && !keeper.node.available && (
        <div className="flex items-start gap-2 rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm leading-6 text-rose-700">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          <span>
            <strong className="font-medium">Node 运行环境未就绪</strong>
            <br />
            {keeper.node.error || "注册协议依赖 Node.js 运行 sentinel sdk.js。"}
          </span>
        </div>
      )}

      {isLoading || !settings ? (
        <div className="flex items-center justify-center py-20 text-stone-400">
          <LoaderCircle className="size-6 animate-spin" />
        </div>
      ) : (
        <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
          {/* 左列：配置 */}
          <div className="space-y-5">
            {/* 水位守护状态 */}
            {keeper && (
              <Card className="rounded-2xl border-stone-200/70 bg-white shadow-none">
                <CardContent className="space-y-3 p-4">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2 text-sm font-medium text-stone-700">
                      <Waves className="size-4" />
                      号池水位
                    </div>
                    <Badge
                      variant="outline"
                      className={cn(
                        "rounded-lg text-[11px]",
                        keeper.enabled && keeper.running
                          ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                          : "border-stone-200 bg-stone-50 text-stone-500",
                      )}
                    >
                      {!keeper.enabled ? "守护未启用" : keeper.running ? "守护运行中" : "守护未运行"}
                    </Badge>
                  </div>
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                    <div className="rounded-xl bg-stone-50 p-2.5">
                      <div className="text-[11px] text-stone-400">有效账号</div>
                      <div className="text-lg font-semibold text-stone-800">
                        {keeper.valid_count}
                        {keeper.target_accounts > 0 && (
                          <span className="text-xs font-normal text-stone-400"> / {keeper.target_accounts}</span>
                        )}
                      </div>
                    </div>
                    <div className="rounded-xl bg-stone-50 p-2.5">
                      <div className="text-[11px] text-stone-400">生图额度</div>
                      <div className="text-lg font-semibold text-stone-800">
                        {keeper.valid_quota}
                        {keeper.target_quota > 0 && (
                          <span className="text-xs font-normal text-stone-400"> / {keeper.target_quota}</span>
                        )}
                      </div>
                    </div>
                    <div className="rounded-xl bg-stone-50 p-2.5">
                      <div className="text-[11px] text-stone-400">在途注册</div>
                      <div className="text-lg font-semibold text-stone-800">{keeper.inflight}</div>
                    </div>
                    <div className="rounded-xl bg-stone-50 p-2.5">
                      <div className="text-[11px] text-stone-400">今日尝试</div>
                      <div className="text-lg font-semibold text-stone-800">
                        {keeper.daily_used}
                        {keeper.daily_limit > 0 && (
                          <span className="text-xs font-normal text-stone-400"> / {keeper.daily_limit}</span>
                        )}
                      </div>
                      <div className="text-[11px] text-stone-400">
                        成功 <span className="text-emerald-600">{keeper.daily_success}</span>
                      </div>
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <Activity className={cn("size-3.5", keeper.enabled && keeper.running ? "text-emerald-500" : "text-stone-400")} />
                    <span className="text-stone-500">
                      {keeper.enabled && keeper.last_round.message === "守护未启用"
                        ? "等待本轮检查"
                        : keeper.cooldown_seconds_left > 0
                        ? `熔断中，剩余 ${Math.ceil(keeper.cooldown_seconds_left / 60)} 分钟`
                        : keeper.last_round.message || "等待首轮检查"}
                    </span>
                    {keeper.consecutive_failures > 0 && keeper.cooldown_seconds_left <= 0 && (
                      <span className="text-amber-600">（连续失败 {keeper.consecutive_failures} 次）</span>
                    )}
                  </div>
                  {keeper.node?.available && (
                    <div className="flex items-center gap-1.5 text-xs text-stone-400">
                      <Terminal className="size-3.5 text-emerald-500" />
                      Node {keeper.node.version} 就绪，sentinel 资源齐备
                    </div>
                  )}
                </CardContent>
              </Card>
            )}

            {/* 邮箱服务 */}
            <Card className="rounded-2xl border-stone-200/70 bg-white shadow-none">
              <CardContent className="space-y-3 p-4">
                <div className="flex items-center gap-2 text-sm font-medium text-stone-700">
                  <Mail className="size-4" />
                  邮箱服务（cloudflare_temp_email）
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs text-stone-500">API 地址（Worker 站点）</label>
                  <Input
                    value={settings.mail_api_base}
                    onChange={(e) => patch("mail_api_base", e.target.value)}
                    placeholder="https://mail.example.com"
                    className="h-10 rounded-xl border-stone-200 bg-white"
                  />
                </div>
                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="space-y-1.5">
                    <label className="text-xs text-stone-500">鉴权方式</label>
                    <Select value={settings.mail_auth_mode} onValueChange={(v) => patch("mail_auth_mode", v)}>
                      <SelectTrigger className="h-10 rounded-xl border-stone-200 bg-white">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="none">无鉴权</SelectItem>
                        <SelectItem value="x-admin-auth">x-admin-auth（管理码）</SelectItem>
                        <SelectItem value="x-api-key">x-api-key</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-xs text-stone-500">
                      密钥{settings.has_mail_auth_key && !settings.mail_auth_key ? "（已配置，留空不变）" : ""}
                    </label>
                    <Input
                      type="password"
                      value={settings.mail_auth_key}
                      onChange={(e) => patch("mail_auth_key", e.target.value)}
                      placeholder="留空保留已保存密钥"
                      className="h-10 rounded-xl border-stone-200 bg-white"
                    />
                  </div>
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs text-stone-500">邮箱域名池（可选，洗牌轮询）</label>
                  <Textarea
                    value={settings.mail_domain}
                    onChange={(e) => patch("mail_domain", e.target.value)}
                    placeholder={"一行一个（也支持逗号/分号）：\na.example.com\nb.example.com"}
                    className="min-h-[76px] rounded-xl border-stone-200 bg-white font-mono text-xs"
                  />
                  <div className="text-xs text-stone-400">
                    {settings.domain_count > 0
                      ? `已解析 ${settings.domain_count} 个域名，按洗牌轮询均匀分配`
                      : "留空则用 Worker 默认域名"}
                  </div>
                </div>
                <div className="grid gap-3 sm:grid-cols-3">
                  <div className="space-y-1.5">
                    <label className="text-xs text-stone-500">验证码等待上限(秒)</label>
                    <Input
                      type="number" min={15} max={600}
                      value={settings.otp_max_wait}
                      onChange={(e) => patch("otp_max_wait", Number(e.target.value))}
                      className="h-10 rounded-xl border-stone-200 bg-white"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-xs text-stone-500">轮询间隔(秒)</label>
                    <Input
                      type="number" min={1} max={30}
                      value={settings.otp_poll_interval}
                      onChange={(e) => patch("otp_poll_interval", Number(e.target.value))}
                      className="h-10 rounded-xl border-stone-200 bg-white"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-xs text-stone-500">验证码总预算(秒)</label>
                    <Input
                      type="number" min={0} max={1800}
                      value={settings.otp_total_budget}
                      onChange={(e) => patch("otp_total_budget", Number(e.target.value))}
                      className="h-10 rounded-xl border-stone-200 bg-white"
                    />
                  </div>
                </div>
                <div className="text-xs text-stone-400">
                  验证码最多重试 3 次；总预算给整个等待阶段（含重试）封顶，0 = 不限。等待期间点「停止」可立即中断。
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs text-stone-500">取 Token 链路</label>
                  <Select
                    value={settings.token_source}
                    onValueChange={(v) => patch("token_source", v as "platform" | "codex")}
                  >
                    <SelectTrigger className="h-10 rounded-xl border-stone-200 bg-white">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="platform">Platform OAuth（web，推荐）</SelectItem>
                      <SelectItem value="codex">Codex OAuth（需接码平台）</SelectItem>
                    </SelectContent>
                  </Select>
                  <div className="text-xs text-stone-400">
                    Platform 全程同一条 OAuth 流，注册完直接换 token，不发第二次登录，因此不会触发手机号验证；
                    落池为 web 类型。Codex 链路会另开干净会话登录一次，常被要求手机号验证，没有接码平台时大概率失败。
                  </div>
                </div>
                <Button
                  variant="outline"
                  className="h-10 w-full rounded-xl border-stone-200 bg-white text-stone-700"
                  onClick={() => void handleTestMail()}
                  disabled={isTesting || !settings.mail_api_base}
                >
                  {isTesting ? <LoaderCircle className="size-4 animate-spin" /> : <ShieldCheck className="size-4" />}
                  测试邮箱服务（随机取域名建箱）
                </Button>
              </CardContent>
            </Card>

            {/* 代理池 */}
            <Card className="rounded-2xl border-stone-200/70 bg-white shadow-none">
              <CardContent className="space-y-2 p-4">
                <div className="flex items-center gap-2 text-sm font-medium text-stone-700">
                  <Bot className="size-4" />
                  代理池
                  <span className="text-xs font-normal text-stone-400">每次注册随机取一个，留空直连</span>
                </div>
                <Textarea
                  value={settings.proxy_pool_text}
                  onChange={(e) => patch("proxy_pool_text", e.target.value)}
                  placeholder={"一行一个，支持：\nsocks5h://user:pass@host:port\nhttp://user:pass@host:port\nuser:pass@host:port（缺 scheme 自动按 http 补）\nhost:port:user:pass"}
                  className="min-h-[96px] rounded-xl border-stone-200 bg-white font-mono text-xs"
                />
                <div className="text-xs text-stone-400">
                  已解析 {settings.proxy_count} 个可用代理（格式非法的行会被自动忽略）
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    variant="outline"
                    className="h-9 rounded-xl border-stone-200 bg-white text-stone-700"
                    onClick={() => void handleTestProxy()}
                    disabled={isTestingProxy || (!settings.proxy_pool_text.trim() && settings.proxy_count === 0)}
                  >
                    {isTestingProxy ? <LoaderCircle className="size-4 animate-spin" /> : <Activity className="size-4" />}
                    测试代理连通性（前 5 条）
                  </Button>
                  {settings.proxy_count === 0 && settings.proxy_pool_text.trim() && (
                    <span className="text-xs text-amber-600">
                      解析出 0 条可用代理，注册将退化为直连
                    </span>
                  )}
                </div>
                {proxyProbe && (
                  <div className="space-y-1.5 rounded-xl border border-stone-200 bg-stone-50 p-2.5">
                    <div className="text-xs text-stone-600">
                      解析 {proxyProbe.parsed} 条，实测 {proxyProbe.tested} 条，
                      <span className={proxyProbe.available > 0 ? "text-emerald-600" : "text-rose-500"}>
                        {" "}可用 {proxyProbe.available} 条
                      </span>
                    </div>
                    {proxyProbe.results.map((r, i) => (
                      <div key={i} className="flex items-start gap-2 font-mono text-[11px]">
                        {r.ok ? (
                          <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-emerald-500" />
                        ) : (
                          <XCircle className="mt-0.5 size-3.5 shrink-0 text-rose-500" />
                        )}
                        <span className="min-w-0 break-all text-stone-600">
                          {r.proxy}
                          {r.ok ? (
                            <span className="ml-1 text-emerald-600">HTTP {r.status}</span>
                          ) : (
                            <span className="ml-1 text-rose-500">{r.error || "不可用"}</span>
                          )}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>

            {/* 水位守护配置 */}
            <Card className="rounded-2xl border-stone-200/70 bg-white shadow-none">
              <CardContent className="space-y-3 p-4">
                <label className="flex cursor-pointer items-start gap-2.5">
                  <Checkbox
                    checked={settings.keeper_enabled}
                    onCheckedChange={(v) => void handleKeeperToggle(Boolean(v))}
                    disabled={isSaving}
                    className="mt-0.5"
                  />
                  <span className="space-y-0.5">
                    <span className="flex items-center gap-2 text-sm font-medium text-stone-700">
                      <Waves className="size-4" />
                      水位守护（自动补水）
                    </span>
                    <span className="block text-xs leading-5 text-stone-500">
                      定期检查有效账号数与生图额度总量，不足时自动注册补齐；在途任务抵扣缺口，避免重复注册。
                    </span>
                  </span>
                </label>
                <div className="grid gap-3 sm:grid-cols-3">
                  {([
                    ["keeper_target_accounts", "目标有效账号数"],
                    ["keeper_target_quota", "目标生图额度"],
                    ["keeper_min_quota", "单号额度门槛"],
                    ["keeper_check_interval", "检查周期(分)"],
                    ["keeper_max_per_round", "单轮最多补"],
                    ["keeper_concurrency", "守护并发数（1-8）"],
                    ["keeper_daily_limit", "每日上限"],
                    ["keeper_fail_threshold", "熔断阈值"],
                    ["keeper_fail_cooldown_minutes", "熔断时长(分)"],
                    ["keeper_estimate_quota", "单号额度估算"],
                  ] as const).map(([key, label]) => (
                    <div key={key} className="space-y-1.5">
                      <label className="text-xs text-stone-500">{label}</label>
                      <Input
                        type="number"
                        min={key === "keeper_concurrency" ? 1 : 0}
                        max={key === "keeper_concurrency" ? 8 : undefined}
                        value={settings[key] as number}
                        onChange={(e) => patch(key, Number(e.target.value))}
                        className="h-10 rounded-xl border-stone-200 bg-white"
                      />
                    </div>
                  ))}
                  <div className="space-y-1.5">
                    <label className="text-xs text-stone-500">统计来源</label>
                    <Input
                      value={settings.keeper_source_type}
                      onChange={(e) => patch("keeper_source_type", e.target.value)}
                      placeholder="codex（留空=全部）"
                      className="h-10 rounded-xl border-stone-200 bg-white"
                    />
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>

          {/* 右列：运行 + 日志 */}
          <div className="space-y-5">
            {/* 本次任务 */}
            <Card className="rounded-2xl border-stone-200/70 bg-white shadow-none">
              <CardContent className="space-y-3 p-4">
                <div className="flex items-center gap-2 text-sm font-medium text-stone-700">
                  <Play className="size-4" />
                  本次批量
                </div>
                <div className="grid gap-3 sm:grid-cols-3">
                  <div className="space-y-1.5">
                    <label className="text-xs text-stone-500">注册数量</label>
                    <Input
                      type="number" min={1} max={200}
                      value={count}
                      onChange={(e) => setCount(e.target.value)}
                      className="h-10 rounded-xl border-stone-200 bg-white"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-xs text-stone-500">并发数（1-8）</label>
                    <Input
                      type="number" min={1} max={8}
                      value={concurrency}
                      onChange={(e) => setConcurrency(e.target.value)}
                      className="h-10 rounded-xl border-stone-200 bg-white"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-xs text-stone-500">统一注册密码</label>
                    <Input
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      placeholder="留空自动生成"
                      className="h-10 rounded-xl border-stone-200 bg-white"
                    />
                  </div>
                </div>
                {job && (
                  <div className="space-y-2 rounded-xl border border-stone-200 bg-stone-50 p-3">
                    <div className="flex items-center justify-between text-sm">
                      <span className="text-stone-600">{job.message}</span>
                      <span className="font-medium text-stone-700">
                        {job.done}/{job.total}
                        <span className="ml-2 text-emerald-600">成功 {job.success}</span>
                        <span className="ml-1 text-rose-500">失败 {job.failed}</span>
                      </span>
                    </div>
                    <div className="h-2 w-full overflow-hidden rounded-full bg-stone-200">
                      <div
                        className="h-full rounded-full bg-gradient-to-r from-emerald-400 to-emerald-600 transition-all duration-300"
                        style={{ width: `${progressPct}%` }}
                      />
                    </div>
                  </div>
                )}
              </CardContent>
            </Card>

            {/* 实时日志 */}
            <Card className="rounded-2xl border-stone-200/70 bg-white shadow-none">
              <CardContent className="space-y-2 p-4">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2 text-sm font-medium text-stone-700">
                    <Terminal className="size-4" />
                    {job?.source === "keeper" ? "守护补水日志" : "实时日志"}
                    {running && <span className="size-1.5 animate-pulse rounded-full bg-emerald-500" />}
                  </div>
                  <div className="flex items-center gap-1.5">
                    <label className="flex cursor-pointer items-center gap-1.5 text-xs text-stone-500">
                      <Checkbox
                        checked={autoScroll}
                        onCheckedChange={(v) => setAutoScroll(Boolean(v))}
                        className="size-3.5"
                      />
                      自动滚动
                    </label>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 rounded-lg px-2 text-xs text-stone-500"
                      onClick={() => setLogs([])}
                      disabled={logs.length === 0}
                    >
                      <Trash2 className="size-3.5" />
                      清空
                    </Button>
                    {streamError && (
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-7 rounded-lg border-amber-200 bg-amber-50 px-2 text-xs text-amber-700"
                        onClick={() => void handleReconnect()}
                      >
                        <RefreshCw className="size-3.5" />
                        重连
                      </Button>
                    )}
                  </div>
                </div>

                {streamError && (
                  <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
                    日志流中断：{streamError}
                  </div>
                )}

                <div
                  ref={logBoxRef}
                  className="h-[520px] overflow-y-auto rounded-xl border border-stone-900/10 bg-stone-950 p-3 font-mono text-[12px] leading-6"
                >
                  {logs.length === 0 ? (
                    <div className="flex h-full items-center justify-center text-stone-600">
                      {running ? "等待日志…" : "暂无注册日志"}
                    </div>
                  ) : (
                    logs.map((entry) => (
                      <div key={entry.seq} className="flex gap-2">
                        <span className="shrink-0 text-stone-600">{formatTime(entry.ts)}</span>
                        <span className={cn("shrink-0", levelStyle[entry.level] ?? "text-stone-400")}>
                          {levelIcon[entry.level] ?? "·"}
                        </span>
                        <span className={cn("min-w-0 whitespace-pre-wrap break-all", levelStyle[entry.level] ?? "text-stone-300")}>
                          {entry.message}
                        </span>
                      </div>
                    ))
                  )}
                </div>

                {/* 结果汇总 */}
                {job && job.results.length > 0 && (
                  <div className="max-h-[160px] space-y-1 overflow-y-auto rounded-xl border border-stone-200 bg-stone-50 p-2.5">
                    {job.results.map((r, i) => (
                      <div key={i} className="flex items-start gap-2 text-xs">
                        {r.ok ? (
                          <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-emerald-500" />
                        ) : (
                          <XCircle className="mt-0.5 size-3.5 shrink-0 text-rose-500" />
                        )}
                        <span className="min-w-0 text-stone-600">
                          {r.email || <span className="text-stone-400">邮箱未创建</span>}
                          {!r.ok && r.error ? <span className="ml-1 text-rose-500">{r.error}</span> : null}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
        </div>
      )}
    </div>
  );
}
