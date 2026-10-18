# -*- coding: utf-8 -*-
"""把 /comfyui_hwmon_hud/stats 挂到 ComfyUI 自带的 aiohttp 服务上。

用 PromptServer.instance.routes（ComfyUI 官方扩展点），不额外起端口、不碰前端路由表。
路由带 comfyui_hwmon_hud 前缀：aiohttp 路由是全局的，前缀避免与其他扩展撞名。
采集在 asyncio.to_thread 里跑：nvidia-smi 是子进程，不能阻塞事件循环。
"""
import asyncio

from aiohttp import web
from server import PromptServer

try:
    from . import stats
except ImportError:          # 独立调试时允许直接 import
    import stats


@PromptServer.instance.routes.get("/comfyui_hwmon_hud/stats")
async def hwmon_hud_stats(request):
    try:
        data = await asyncio.to_thread(stats.collect)
        return web.json_response(data, headers={"Cache-Control": "no-store"})
    except Exception as exc:      # 采集异常也必须回结构化结果，前端好降级显示
        return web.json_response({"error": str(exc)}, status=200,
                                 headers={"Cache-Control": "no-store"})
