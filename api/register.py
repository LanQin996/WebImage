# -*- coding: utf-8 -*-
"""注册协议 API 路由：邮箱/代理池配置 + 批量注册任务。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.support import require_admin
from services.register_config import (
    get_public_register_settings,
    get_register_settings,
    update_register_settings,
)
from services.register_protocol.proxy_pool import pick_proxy
from services.register_protocol.sentinel_runner import node_health
from services.register_protocol.service import register_manager
from services.register_runner import pool_keeper, save_account


class RegisterSettingsUpdateRequest(BaseModel):
    enabled: bool | None = None
    mail_api_base: str | None = None
    mail_auth_mode: str | None = None
    mail_auth_key: str | None = None
    mail_domain: str | None = None
    proxy_pool_text: str | None = None
    otp_max_wait: int | None = None
    otp_poll_interval: int | None = None
    auto_locale_from_ip: bool | None = None
    anon_bootstrap: bool | None = None
    send_sentinel_on_email_otp_validate: bool | None = None
    default_count: int | None = None
    default_concurrency: int | None = None
    fixed_password: str | None = None
    # 水位守护
    keeper_enabled: bool | None = None
    keeper_target_accounts: int | None = None
    keeper_target_quota: int | None = None
    keeper_min_quota: int | None = None
    keeper_estimate_quota: int | None = None
    keeper_check_interval: int | None = None
    keeper_max_per_round: int | None = None
    keeper_concurrency: int | None = None
    keeper_daily_limit: int | None = None
    keeper_source_type: str | None = None
    keeper_fail_threshold: int | None = None
    keeper_fail_cooldown_minutes: int | None = None


class RegisterStartRequest(BaseModel):
    count: int = 1
    concurrency: int = 1
    password: str = ""


class RegisterTestMailRequest(BaseModel):
    mail_api_base: str = ""
    mail_auth_mode: str = "none"
    mail_auth_key: str = ""
    mail_domain: str = ""
    use_proxy: bool = False


class RegisterTestProxyRequest(BaseModel):
    proxy_pool_text: str = ""
    limit: int = 5


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/register/settings")
    async def get_settings(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"settings": get_public_register_settings()}

    @router.post("/api/register/settings")
    async def save_settings(body: RegisterSettingsUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "没有需要保存的改动"})
        settings = update_register_settings(updates)
        pool_keeper.wake()
        return {"settings": settings}

    @router.post("/api/register/test-proxy")
    async def test_proxy(body: RegisterTestProxyRequest, authorization: str | None = Header(default=None)):
        """逐条实测代理池：出口 IP + 能否访问 chatgpt.com（403 表示被 Cloudflare 拦）。

        注册失败最常见的原因就是出口 IP 被拦，这里直接给出每条线路的真实判定。
        """
        require_admin(authorization)
        from services.register_protocol.proxy_pool import mask_proxy, parse_proxy_pool
        from services.register_protocol.session import BrowserSession

        saved = get_register_settings()
        raw = (body.proxy_pool_text or "").strip() or str(saved.get("proxy_pool_text") or "")
        pool = parse_proxy_pool(raw)
        if not pool:
            raise HTTPException(status_code=400, detail={"error": "代理池为空或全部格式非法，没有可测试的线路"})

        sample = pool[: max(1, min(20, int(body.limit or 5)))]

        def _probe_one(proxy_url: str) -> dict:
            item: dict = {"proxy": mask_proxy(proxy_url), "ok": False, "ip": "", "status": 0, "error": ""}
            session = None
            try:
                session = BrowserSession(proxy=proxy_url, fingerprint_seed=f"probe-{proxy_url[-12:]}")
                headers = session.get_chatgpt_navigate_headers(referer="")
                resp = session.get(
                    "https://chatgpt.com/auth/login",
                    headers=headers,
                    allow_redirects=True,
                    timeout=30,
                )
                item["status"] = int(resp.status_code)
                item["ok"] = 200 <= int(resp.status_code) < 400
                if not item["ok"]:
                    item["error"] = f"HTTP {resp.status_code}（疑似被 Cloudflare 拦截）"
            except Exception as exc:
                item["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            finally:
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
            return item

        results: list[dict] = []
        for proxy_url in sample:
            results.append(await run_in_threadpool(_probe_one, proxy_url))

        return {
            "parsed": len(pool),
            "tested": len(results),
            "available": sum(1 for r in results if r["ok"]),
            "results": results,
        }

    @router.post("/api/register/test-mail")
    async def test_mail(body: RegisterTestMailRequest, authorization: str | None = Header(default=None)):
        """用给定（或已保存的）邮箱配置实测创建一个临时邮箱，验证连通性。"""
        require_admin(authorization)
        from services.register_protocol.mail_provider import CfTempMailClient, MailProviderError

        saved = get_register_settings()
        api_base = (body.mail_api_base or "").strip() or saved["mail_api_base"]
        auth_mode = (body.mail_auth_mode or "").strip() or saved["mail_auth_mode"]
        auth_key = (body.mail_auth_key or "").strip() or saved["mail_auth_key"]
        domain = (body.mail_domain or "").strip() or saved["mail_domain"]
        if not api_base:
            raise HTTPException(status_code=400, detail={"error": "请先填写 cloudflare_temp_email 的 API 地址"})

        proxy = ""
        if body.use_proxy:
            pool = saved.get("proxy_pool") or []
            proxy = pick_proxy(pool) or ""

        def _run() -> dict:
            client = CfTempMailClient(
                api_base, auth_mode=auth_mode, auth_key=auth_key, domain=domain, proxy=proxy or None
            )
            mailbox = client.create_mailbox()
            return {"ok": True, "address": mailbox.address, "via_proxy": bool(proxy)}

        try:
            result = await run_in_threadpool(_run)
        except MailProviderError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"error": f"{type(exc).__name__}: {str(exc)[:240]}"}) from exc
        return {"result": result}

    @router.post("/api/register/start")
    async def start_register(body: RegisterStartRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        settings = get_register_settings()
        if not settings["ready"]:
            raise HTTPException(
                status_code=400,
                detail={"error": "注册服务未就绪：请先配置 cloudflare_temp_email 的 API 地址"},
            )
        count = max(1, min(200, int(body.count or 1)))
        concurrency = max(1, min(8, int(body.concurrency or 1)))
        password = (body.password or "").strip() or str(settings.get("fixed_password") or "")

        # Node 是 sentinel 的硬依赖；提前拦下来，避免跑到一半才失败一堆账号。
        node = node_health()
        if not node["available"]:
            raise HTTPException(
                status_code=400,
                detail={"error": f"Node 运行环境未就绪：{node['error'] or '请检查 Node 安装'}"},
            )

        job = register_manager.start_batch(
            count=count,
            settings=settings,
            concurrency=concurrency,
            password=password or None,
            on_account=save_account,
        )
        return {"job_id": job.job_id, "total": job.total}

    @router.get("/api/register/keeper")
    async def keeper_status(authorization: str | None = Header(default=None)):
        """水位守护实时状态：有效账号/额度、在途、缺口与熔断。"""
        require_admin(authorization)
        return {"keeper": pool_keeper.status()}

    @router.get("/api/register/progress/{job_id}")
    async def get_progress(job_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        job = register_manager.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail={"error": "任务不存在或已结束"})
        return job.snapshot()

    @router.get("/api/register/jobs/latest")
    async def latest_register_job(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        job = register_manager.latest_job()
        return {"job": job.snapshot() if job else None}

    @router.get("/api/register/logs/{job_id}")
    async def stream_logs(
        job_id: str,
        after: int = 0,
        authorization: str | None = Header(default=None),
    ):
        """实时日志 SSE：先补 after 之后的历史日志，再持续推送增量直到任务结束。

        job 结束后会主动关闭连接（客户端可重连用 after=last_seq 续传）。
        """
        require_admin(authorization)
        job = register_manager.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail={"error": "任务不存在或已结束"})

        def _sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        async def event_stream():
            seq = max(0, int(after))
            idle_rounds = 0
            try:
                while True:
                    logs, finished = await run_in_threadpool(job.wait_for_logs, seq, 15.0)
                    if logs:
                        idle_rounds = 0
                        for item in logs:
                            seq = max(seq, int(item.get("seq") or 0))
                            yield _sse("log", item)
                    else:
                        idle_rounds += 1
                        # 心跳：保持连接不被中间层掐断
                        yield ": keep-alive\n\n"
                    snapshot = await run_in_threadpool(job.snapshot)
                    yield _sse("status", snapshot)
                    if finished and not logs:
                        yield _sse("done", snapshot)
                        return
                    if finished:
                        # 还有日志没推完时继续推，下一轮自然会收尾
                        continue
                    if idle_rounds > 240:  # 约 1 小时无进展，主动收尾
                        yield _sse("done", snapshot)
                        return
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/api/register/stop/{job_id}")
    async def stop_register(job_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not register_manager.stop_job(job_id):
            raise HTTPException(status_code=404, detail={"error": "任务不存在或已结束"})
        return {"ok": True}

    return router
