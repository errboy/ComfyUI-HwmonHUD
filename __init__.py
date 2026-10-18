# -*- coding: utf-8 -*-
"""ComfyUI-HwmonHUD —— 顶栏硬件监视条（纯 UI 扩展，不提供工作流节点）。

显示：判定状态 + GPU 利用率 / 温度 / 功耗 + 内存利用率 + CPU 负载。
数据来自本包注册的 GET /comfyui_hwmon_hud/stats（见 api_routes.py），前端在 web/hud.js。

合规要点（对照 ComfyUI 官方自定义节点规范）：
  · WEB_DIRECTORY 导出并在 __all__ 中声明；web/*.js 由前端自动加载
  · 无节点类：NODE_CLASS_MAPPINGS 为空，属于官方文档所述「仅客户端」扩展
  · 路由走 PromptServer.instance.routes，带唯一前缀（aiohttp 路由是全局表）
  · 日志走 logging（logger 名带 [ComfyUI-HwmonHUD] 前缀），不用 print
"""
import logging

__version__ = "0.1.0"

WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

logging.getLogger(__name__).addHandler(logging.NullHandler())
_log = logging.getLogger("[ComfyUI-HwmonHUD]")

# 路由注册失败不能拖垮整个 ComfyUI：记一条警告后照常启动（前端会显示"离线"）
try:
    from . import api_routes  # noqa: F401
except Exception as exc:      # pragma: no cover
    _log.warning("路由注册跳过：%s", exc)

__all__ = ["WEB_DIRECTORY", "NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "__version__"]
