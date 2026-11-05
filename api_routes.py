# -*- coding: utf-8 -*-
"""把 /comfyui_hwmon_hud/stats 挂到 ComfyUI 自带的 aiohttp 服务上。

用 PromptServer.instance.routes（ComfyUI 官方扩展点），不额外起端口、不碰前端路由表。
路由带 comfyui_hwmon_hud 前缀：aiohttp 路由是全局的，前缀避免与其他扩展撞名。
采集在 asyncio.to_thread 里跑：nvidia-smi 是子进程，不能阻塞事件循环。

★ 2026-11-01：额外把「队列里有没有任务在跑」传给采集层。
  判定"假忙卡死"必须知道队列状态——没有它，「有任务、但这类负载耗电低」
  （音乐生成 util 99% / 87W）会被当成"没活儿装忙"，实测 1373 条误报。
  取不到队列就传 None，采集层退回旧行为（不因采集失败而卡判定）。
"""
import asyncio

from aiohttp import web
from server import PromptServer

try:
    from . import stats
except ImportError:          # 独立调试时允许直接 import
    import stats


async def _queue_busy():
    """ComfyUI 队列是否有任务（running 或 pending 非空）。取不到返回 None。

    ★ 2026-11-01：优先用 get_current_queue_volatile（浅拷贝，不 deepcopy）。
      前端每 1.5 秒问一次，深拷贝整个队列没必要。常驻采样线程走的是 stats.probe_queue()，
      同一套字段读取逻辑，两处口径一致。
    """
    try:
        pq = PromptServer.instance.prompt_queue
        getter = getattr(pq, "get_current_queue_volatile", None) or pq.get_current_queue
        cur = getter()
        if asyncio.iscoroutine(cur):     # 新版本可能是 async
            cur = await cur
        if not cur:
            return None
        running, pending = cur[0], cur[1]
        return bool(running or pending)
    except Exception:
        return None


@PromptServer.instance.routes.get("/comfyui_hwmon_hud/stats")
async def hwmon_hud_stats(request):
    try:
        busy = await _queue_busy()
        data = await asyncio.to_thread(stats.collect, busy)
        return web.json_response(data, headers={"Cache-Control": "no-store"})
    except Exception as exc:      # 采集异常也必须回结构化结果，前端好降级显示
        return web.json_response({"error": str(exc)}, status=200,
                                 headers={"Cache-Control": "no-store"})
