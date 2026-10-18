// ComfyUI-HwmonHUD 前端（独立实现，未复用任何第三方监控节点的代码）
// 职责：在顶栏"运行/队列"按钮同一排挂一个监视条，轮询 /comfyui_hwmon_hud/stats 并按等级上色。
// 兼容策略：优先用 ComfyUI 官方 app.menu 按钮组挂载；老版/未来版依次回退到
//           settingsGroup、#queue-button、右下角浮窗——任何一个失败都不影响其它流程。
//
// 防重名：DOM id / 扩展名 / 路由 全部带 comfyui_ 前缀（见 README「命名规范」）。

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const HUD_ID = "comfyui-hwmon-hud";
const POLL_MS = 1500;

// 判定/配色全部在服务端算（stats.py），前端只做颜色名 → 十六进制映射。
// 这样"终端 hwmon 面板 / 顶栏 HUD"永远同一套阈值，不会各自漂移。
const COL = {
  gray: "#8a8f98",
  green: "#3fb950",
  yellow: "#d29922",
  red: "#f85149",
  dim: "#6e7681",
};

const CSS = `
#${HUD_ID}{
  display:flex; align-items:center; gap:6px; height:30px; padding:0 8px; margin:0 2px;
  border:1px solid var(--border-color, rgba(128,128,128,.35));
  border-radius:6px;
  background:var(--secondary-background, rgba(128,128,128,.12));
  color:var(--base-foreground, inherit);
  font:12px/1 ui-monospace, "Cascadia Mono", Consolas, monospace;
  font-variant-numeric:tabular-nums; white-space:nowrap; user-select:none; cursor:default;
}
#${HUD_ID} .hud-l{opacity:.55; margin-right:1px}
#${HUD_ID} .hud-v{font-weight:600}
#${HUD_ID} .hud-sep{opacity:.22}
#${HUD_ID} .hud-dot{width:7px; height:7px; border-radius:50%; display:inline-block; flex:0 0 auto}
#${HUD_ID}.hud-narrow .hud-l,
#${HUD_ID}.hud-narrow .hud-sep{display:none}
#${HUD_ID}.hud-float{
  position:fixed; right:14px; bottom:14px; z-index:12000; height:32px;
  box-shadow:0 4px 14px rgba(0,0,0,.35);
}
`;

function injectStyle() {
  if (document.getElementById(HUD_ID + "-css")) return;
  const st = document.createElement("style");
  st.id = HUD_ID + "-css";
  st.textContent = CSS;
  document.head.appendChild(st);
}

function build() {
  const box = document.createElement("div");
  box.id = HUD_ID;
  box.title = "ComfyUI-HwmonHUD · 硬件监视（加载中…）";
  box.innerHTML =
    '<span class="hud-dot" data-k="dot" style="background:' + COL.dim + '"></span>' +
    '<span class="hud-v hud-verdict" data-k="verdict">--</span>' +
    '<span class="hud-sep">│</span>' +
    '<span class="hud-l">GPU</span>' +
    '<span class="hud-v" data-k="util">--</span>' +
    '<span class="hud-v" data-k="temp">--</span>' +
    '<span class="hud-v" data-k="power">--</span>' +
    '<span class="hud-sep">│</span>' +
    '<span class="hud-l">内存</span><span class="hud-v" data-k="ram">--</span>' +
    '<span class="hud-sep">│</span>' +
    '<span class="hud-l">CPU</span><span class="hud-v" data-k="cpu">--</span>';
  return box;
}

// 2026-10-18 实测修正（新版前端 1.55.17）：
//   app.menu（ComfyAppMenu）在新版 UI 里是**遗留隐藏菜单**（祖先 .comfy-menu display:none），
//   插进去会得到 0×0 的不可见元素。真正可见的"运行/队列"按钮是 .queue-button-group。
//   因此锚点优先级：可见 queue-button-group → 可见 app.menu 设置组 → 旧 #queue-button → 浮窗。
function isVisible(el) {
  if (!el || !el.isConnected) return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}

function mount() {
  const existing = document.getElementById(HUD_ID);
  if (existing) {
    if (isVisible(existing)) return true;
    // 在 DOM 里却不可见（被塞进 display:none 的遗留容器）→ 拆掉重挂
    existing.remove();
  }
  injectStyle();

  let box = null;
  // 1) 新版顶栏：可见的"运行/队列"按钮组（插在它左边，同一排）
  const queueGroup = document.querySelector(".queue-button-group");
  if (isVisible(queueGroup) && queueGroup.parentElement) {
    box = build();
    queueGroup.parentElement.insertBefore(box, queueGroup);
  }
  // 2) 官方 app.menu（仅在可见时可用；老版 UI 走这里）
  const menu = app && app.menu;
  const settingsEl = menu && menu.settingsGroup && menu.settingsGroup.element;
  if (!box && isVisible(settingsEl)) {
    box = build();
    settingsEl.insertAdjacentElement("beforebegin", box);
  }
  // 3) 更老的 #queue-button
  if (!box) {
    const qb = document.getElementById("queue-button");
    if (isVisible(qb)) {
      box = build();
      qb.insertAdjacentElement("afterend", box);
    }
  }
  if (box) {
    layout();
    return true;
  }
  return false;
}

function mountFloat() {
  if (document.getElementById(HUD_ID)) return;
  injectStyle();
  const box = build();
  box.classList.add("hud-float");
  document.body.appendChild(box);
  layout();
}

function layout() {
  const box = document.getElementById(HUD_ID);
  if (box) box.classList.toggle("hud-narrow", window.innerWidth < 1100);
}

function fmt(v, unit, digits) {
  if (v == null || isNaN(v)) return "--";
  const n = digits ? Number(v).toFixed(digits) : Math.round(v);
  return n + unit;
}

function paint(s) {
  const box = document.getElementById(HUD_ID);
  if (!box) return;
  const cc = s.colors || {};                       // 服务端下发的颜色名
  const dotColor = COL[(s.verdict && s.verdict.color) || "gray"] || COL.dim;
  const set = (k, text, colorName) => {
    const el = box.querySelector('[data-k="' + k + '"]');
    if (!el) return;
    el.textContent = text;
    el.style.color = colorName && COL[colorName] ? COL[colorName] : "";
  };
  const dot = box.querySelector('[data-k="dot"]');
  if (dot) dot.style.background = dotColor;
  set("verdict", (s.verdict && s.verdict.text) || "--", (s.verdict && s.verdict.color) || "gray");
  const g = s.gpu || {};
  set("util", fmt(g.util, "%"), cc.util);
  set("temp", fmt(g.temp, "℃"), cc.temp);
  set("power", fmt(g.power, "W"), cc.power);
  set("ram", fmt(s.ram && s.ram.percent, "%"), cc.ram);
  set("cpu", fmt(s.cpu && s.cpu.percent, "%"), cc.cpu);

  const lines = [];
  lines.push("GPU " + (g.name || "未知") + "：利用率 " + fmt(g.util, "%") +
             " · 温度 " + fmt(g.temp, "℃") + " · 功耗 " + fmt(g.power, "W") +
             " · 显存 " + fmt(g.vram_used_gb, "", 1) + "/" + fmt(g.vram_total_gb, "", 1) + " GB");
  if (s.ram) lines.push("内存 " + fmt(s.ram.used_gb, "", 1) + "/" + fmt(s.ram.total_gb, "", 1) +
                        " GB（" + fmt(s.ram.percent, "%") + "）");
  lines.push("CPU 负载 " + fmt(s.cpu && s.cpu.percent, "%"));
  if (s.verdict) lines.push("判定 " + s.verdict.text);
  if (s.verdict && s.verdict.why) lines.push("依据 " + s.verdict.why);
  const bl = s.baseline || {};
  if (bl.idle_power != null || bl.idle_temp != null) {
    lines.push("本机基线 底温 " + fmt(bl.idle_temp, "℃") +
               " · 底功耗 " + fmt(bl.idle_power, "W") +
               " · 峰值 " + fmt(bl.peak_power, "W") +
               (bl.ram_work != null ? " · 干活常态内存 " + Math.round(bl.ram_work) + "%" : ""));
  }
  lines.push("（ComfyUI-HwmonHUD · " + (POLL_MS / 1000) + "s 刷新）");
  box.title = lines.join("\n");
}

function offline(msg) {
  const box = document.getElementById(HUD_ID);
  if (!box) return;
  box.querySelectorAll(".hud-v").forEach((el) => { el.textContent = "--"; });
  const dot = box.querySelector('[data-k="dot"]');
  if (dot) dot.style.background = COL.dim;
  box.title = "ComfyUI-HwmonHUD · 离线：" + (msg || "无法访问 /comfyui_hwmon_hud/stats");
}

async function tick() {
  const el = document.getElementById(HUD_ID);
  if (!isVisible(el)) {
    // 不在 DOM、或被塞进不可见容器 → 重新挂载（Vue 重渲染后自动补回）
    if (el) el.remove();
    mount();
    layout();
  }
  try {
    const resp = await api.fetchApi("/comfyui_hwmon_hud/stats", { cache: "no-store" });
    if (!resp || !resp.ok) throw new Error("HTTP " + (resp && resp.status));
    const s = await resp.json();
    if (s && s.error) throw new Error(s.error);
    paint(s);
  } catch (e) {
    offline(e && e.message);
  }
}

app.registerExtension({
  name: "ComfyUI.HwmonHUD",
  async setup() {
    injectStyle();
    // 顶栏由 Vue 异步构建：短轮询找锚点，找到即停；15s 仍未挂上才退到浮窗
    let tries = 0;
    const timer = setInterval(() => {
      if (mount()) {
        clearInterval(timer);
      } else if (++tries > 60) {
        clearInterval(timer);
        mountFloat();
      }
    }, 250);
    window.addEventListener("resize", layout);
    tick();
    setInterval(tick, POLL_MS);
  },
});
