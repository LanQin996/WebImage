# -*- coding: utf-8 -*-
"""号池水位守护：按"有效账号数 + 生图额度总量"双水位自动补充注册。

设计要点：
- 有效口径：status 正常（非 禁用/限流/异常）且 quota >= keeper_min_quota。
  quota 来自远端 conversation/init 的 image_gen.remaining，是真实生图额度。
- 在途抵扣：正在注册但未结束的数量同时抵扣账号缺口与额度缺口，避免每轮重复发起。
- 双水位：账号数不足或额度总量不足，任一触发即补，取两者所需较大值。
- 熔断：连续失败达到阈值后暂停一段时间，避免邮箱服务/风控异常时持续空烧。
- 日限额：每日发起注册数受 keeper_daily_limit 约束。
"""
from __future__ import annotations

import logging
import threading
import time

from services.account_service import account_service
from services.register_config import get_register_settings
from services.register_protocol.sentinel_runner import node_health
from services.register_protocol.service import RegisterStopRequested, register_manager

logger = logging.getLogger(__name__)

_INVALID_STATUS = {"禁用", "限流", "异常"}


def save_account(payload: dict) -> dict:
    """注册成功后的落池动作：写入号池并立即刷新拿真实生图额度。

    注意 type 不能写死 codex：account_service._prepare_account_payload 见到
    type=codex 会把它改写成 source_type=codex（那是 CPA 导出格式的语义），
    platform 链路拿到的 web token 会被错误归类。注册出口仅用于注册过程，
    落池时不传代理，由现有账号代理池为新账号分配绑定。
    """
    source_type = str(payload.get("source_type") or "web").strip().lower() or "web"
    item: dict = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", ""),
        "id_token": payload.get("id_token", ""),
        "account_id": payload.get("account_id", ""),
        "email": payload.get("email", ""),
        "source_type": source_type,
        "plan_type": payload.get("plan_type", ""),
    }
    # 保存注册密码：供后续自动重登（auto_relogin_after_refresh）使用。
    if payload.get("registration_password"):
        item["password"] = payload["registration_password"]
    result = account_service.add_account_items([item])
    # 立即刷新一次，把 quota/restore_at/status 填成远端真实值，水位判断才有依据。
    try:
        account_service.refresh_accounts([payload["access_token"]])
    except Exception:
        logger.warning("[水位守护] 新号刷新失败，额度暂不可知", exc_info=True)
    return result


def _is_valid(account: dict, min_quota: int, source_type: str) -> bool:
    if str(account.get("status") or "") in _INVALID_STATUS:
        return False
    if source_type and str(account.get("source_type") or "web").lower() != source_type.lower():
        return False
    if not account_service._quota_known(account):
        # 额度未知的账号按「有效」计：它只是还没探测成功，
        # 当成 0 额度会把水位守护逼进反复补注册的循环。
        return True
    return int(account.get("quota") or 0) >= max(0, min_quota)


def _valid_snapshot(min_quota: int, source_type: str) -> tuple[int, int, int]:
    """返回 (有效账号数, 有效额度总和, 观测到的单号平均额度)。"""
    accounts = account_service.list_accounts()
    valid = [a for a in accounts if _is_valid(a, min_quota, source_type)]
    quotas = [int(a.get("quota") or 0) for a in valid if account_service._quota_known(a)]
    quotas = [q for q in quotas if q > 0]
    avg = max(1, round(sum(quotas) / len(quotas))) if quotas else 0
    return len(valid), sum(quotas), avg


class PoolKeeper:
    """号池水位守护线程（内存态状态）。"""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._cooldown_until = 0.0
        self.last_round: dict = {"at": 0.0, "message": "未运行"}

    # ---- 状态查询 ----

    def status(self) -> dict:
        s = get_register_settings()
        with self._lock:
            failures = self._consecutive_failures
            cooldown_left = max(0.0, self._cooldown_until - time.time())
        valid_count, valid_quota, avg_quota = _valid_snapshot(s["keeper_min_quota"], s["keeper_source_type"])
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "enabled": bool(s["keeper_enabled"]),
            "valid_count": valid_count,
            "valid_quota": valid_quota,
            "avg_quota": avg_quota,
            "inflight": register_manager.inflight_count(),
            "daily_used": register_manager.daily_used(),
            "daily_success": register_manager.daily_success(),
            "daily_limit": s["keeper_daily_limit"],
            "target_accounts": s["keeper_target_accounts"],
            "target_quota": s["keeper_target_quota"],
            "consecutive_failures": failures,
            "cooldown_seconds_left": int(cooldown_left),
            "last_round": dict(self.last_round),
            "node": node_health(),
        }

    # ---- 生命周期 ----

    def start(self, stop_event: threading.Event) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop = stop_event

        def worker() -> None:
            logger.info("[水位守护] 线程启动")
            while not self._stop.is_set():
                interval = 300
                try:
                    interval = self._run_round()
                except Exception:
                    logger.exception("[水位守护] 本轮异常")
                self._wake.wait(max(15, interval))
                self._wake.clear()
            logger.info("[水位守护] 线程退出")

        self._thread = threading.Thread(target=worker, name="register-pool-keeper", daemon=True)
        self._thread.start()
        return self._thread

    def wake(self) -> None:
        self._wake.set()

    def _note_round(self, message: str) -> None:
        self.last_round = {"at": time.time(), "message": message}

    def _on_round(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
                s = get_register_settings()
                threshold = int(s["keeper_fail_threshold"])
                if threshold > 0 and self._consecutive_failures >= threshold:
                    self._cooldown_until = time.time() + int(s["keeper_fail_cooldown_minutes"]) * 60
                    logger.warning(
                        "[水位守护] 连续失败 %s 次，熔断 %s 分钟",
                        self._consecutive_failures, s["keeper_fail_cooldown_minutes"],
                    )

    # ---- 单轮水位判定 ----

    def _run_round(self) -> int:
        s = get_register_settings()
        interval = max(1, int(s["keeper_check_interval"])) * 60

        if not s["keeper_enabled"]:
            self._note_round("守护未启用")
            return interval
        if not s["ready"]:
            self._note_round("邮箱服务未配置，未就绪")
            return interval

        with self._lock:
            cooldown_left = self._cooldown_until - time.time()
        if cooldown_left > 0:
            self._note_round(f"熔断中，剩余 {int(cooldown_left)} 秒")
            return min(interval, int(cooldown_left) + 5)

        valid_count, valid_quota, observed_avg = _valid_snapshot(s["keeper_min_quota"], s["keeper_source_type"])
        inflight = register_manager.inflight_count()
        # 额度未知的账号不计入 valid_quota，但按单号估算额度补回总盘，
        # 否则会把总量低估成 0 而反复补注册。
        unknown_quota = account_service.unknown_quota_count()

        # 单号额度估算：优先用池内观测均值，池空时用配置的兜底值。
        est_quota = observed_avg or max(1, int(s["keeper_estimate_quota"]))

        target_accounts = int(s["keeper_target_accounts"])
        target_quota = int(s["keeper_target_quota"])

        gap_accounts = max(0, target_accounts - (valid_count + inflight)) if target_accounts > 0 else 0
        if target_quota > 0:
            projected = valid_quota + (unknown_quota + inflight) * est_quota
            missing = max(0, target_quota - projected)
            gap_quota = -(-missing // est_quota)  # 向上取整
        else:
            gap_quota = 0

        need = max(gap_accounts, gap_quota)
        if need <= 0:
            self._note_round(
                f"水位充足：有效 {valid_count} 个 / {valid_quota} 额度，在途 {inflight}"
            )
            return interval

        daily_limit = int(s["keeper_daily_limit"])
        daily_left = register_manager.daily_remaining(daily_limit)
        if daily_left == 0:
            self._note_round(f"已达每日上限 {daily_limit}，暂停补给")
            return interval

        if daily_left > 0:
            need = min(need, daily_left)
        need = min(need, max(1, int(s["keeper_max_per_round"])))

        register_manager.start_batch(
            count=need,
            settings=s,
            concurrency=int(s["keeper_concurrency"]),
            source="keeper",
            password=str(s.get("fixed_password") or "") or None,
            on_account=save_account,
            on_round=self._on_round,
        )
        register_manager.prune_finished_jobs()
        self._note_round(
            f"补水 {need} 个（账号缺口 {gap_accounts}、额度缺口 {gap_quota}；"
            f"现有 {valid_count} 个 / {valid_quota} 额度，在途 {inflight}）"
        )
        logger.info("[水位守护] %s", self.last_round["message"])
        return interval


pool_keeper = PoolKeeper()


def start_pool_keeper(stop_event: threading.Event) -> threading.Thread:
    return pool_keeper.start(stop_event)
