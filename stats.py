# -*- coding: utf-8 -*-
"""HwmonHUD 数据采集层（独立实现，不依赖任何第三方监控节点）。

指标（五项）：GPU 利用率 / GPU 温度 / GPU 功耗 / 内存利用率 / CPU 负载
判定：**滑动窗口 + 四态状态机**（2026-11-01 从"单点阈值"升级而来）。

为什么要窗口：单点判定会把「采样噪声」当「状态」。实测 14 个任务段里 6 个（43%）
在任务内部出现过"瞬间判空闲"，这些跌落 P90=4s、最长 8s，而 GPU 其实一直在算同一帧。
改用最近 15 秒的中位数后，翻脸任务降为 0。

为什么四个状态：旧的四类（空闲/假忙卡死/真·生产驴/低负载）名字对不上实际场景，
"低负载/加载中"把两种完全不同的事混在一起。现在拆成：

    IDLE     队列空                       —— 没人用
    LOADING  队列有活 + 显存在涨/刚起步    —— 正在往显存搬模型
    WORKING  队列有活 + 功耗或温度明显高于底噪 —— 在算（不管快慢）
    STALLED  队列有活 + 长时间什么都不变    —— 卡住了

★ 卡死与"慢"的区别（2026-11-01 用户指出，物理上成立）：
    慢   = 效率低但确实在烧电：音乐低相 100W，是空闲底噪(18W)的 5.5 倍，温度高 9℃
    卡死 = 不产热：长时间高占用 + 功耗贴底噪 + 温度贴底温
  但"GPU 自旋型卡死"与"轻负载"在电平上只差 2~4℃，靠电平分不开 →
  真正的判据是**静止**：所有量 30 秒内变化趋近于零，且持续 60 秒以上。
  所以长时段的价值不是"电平更准"，而是"能看出有没有变化"。

与 tools/hwmon/hwmon.py 的关系：阈值 TH 必须两边同步；窗口/状态机只在本节点内实现
（本地 CLI 是单次体检，没有时间窗，采集脚本读的就是本接口）。
"""
import ctypes
import hashlib
import json
import os
import subprocess
import threading
import time
from collections import deque

try:
    import psutil
except Exception:          # 仅在极简环境触发
    psutil = None

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "_data")
BASELINE_PATH = os.path.join(DATA_DIR, "baseline.json")
TASKBOOK_PATH = os.path.join(DATA_DIR, "task_durations.json")

TTL = 1.0                  # 秒；前端 1.5s 轮询，后端再缓存一层

# 与 tools/hwmon/hwmon.py 的 CFG 同步（改一边必须改另一边）
#
# ★ 2026-11-01 按 4170 条真实采样重标定（RTX 5060 Ti，功耗墙 180W / 硬件上限 216W）：
#     image  稳态：util 100% / 功耗 160W / 温度 59℃
#     music3 稳态：util 99%  / 功耗 87W（低平台）~174W（高平台）/ 温度 44~63℃
#   旧值全是按"只有出图"标定的，结果音乐任务 util 99% + 功耗 87W 落在 real_power=110
#   之下 → 被判"假忙卡死"，实测 1373 条（占全部样本 1/3），全是误报。
#   本次依据见 tools/hwmon/logs/HUD参数实测报告.md。
TH = {
    "idle_util": 35.0, "idle_power": 45.0,   # 空闲实测：util≈20%（桌面合成也吃 GPU）、功耗 P95＝25W
    "fake_util": 92.0,                       # 最弱长任务 util 中位数 99%
    "real_power": 96.0, "real_temp": 49.0,   # 96＝最弱长任务功耗中位 138W×0.7
    "alert_temp": 73.0,                      # 实测最高 65℃ + 8℃ 边际（旧 83℃ 永不触发）
    "rise_temp": 8.0,          # 温度高于空闲基线 ≥ 此值 → 温度侧证据（实测 Δ16℃ 取半）
    "rise_power_frac": 0.61,   # 功耗在(基线→峰值)区间的位置 ≥ 此比例 → 功耗侧证据
    "temp_abs_min_util": 89.0, # 绝对温度兜底时 util 至少这么高（防加载期误判）
}
# ⚠ idle_util 取 35 而不是分析脚本建议的 60：
#   脚本建议来自「空闲样本 util P99=55%」，实测复核发现那批样本混了任务结束的下降沿。
#   真正站得住的依据是另一条——本机**空闲常态 util 就是 20%**（桌面/浏览器吃 GPU 加速），
#   旧值 15 会让空闲一直显示「低负载/加载中」，且基线永远学不到（u<15 才学）。
#   35 夹在「空闲峰值 ~25%」与「出图稳态 P5 47%」之间；若提到 60，
#   util 15~60% 的常态工作会被判"空闲"，副作用更大。

# ---- 状态机参数（仅供本节点使用，不进 CFG） ----
SM = {
    "win_s": 15.0,             # 主判定窗口：实测跌落 P100=8s，15s 窗能全覆盖
    "freeze_win_s": 30.0,      # 冻结检测窗口（静止要看足够长才成立）
    "freeze_min_span": 22.0,   # 窗口内样本至少要覆盖这么久才允许判"静止"
    "eps_util": 3.0,           # 以下四个是"没变化"的容差
    "eps_power": 4.0,
    "eps_temp": 1.0,
    "eps_vram": 64.0,          # MB
    "stall_after_s": 60.0,     # 连续静止多久判卡死（首次报警 ≈ 90s）
    "vram_rise_mb": 800.0,     # 15s 内显存涨这么多 → 正在加载
    "work_power_ratio": 3.0,   # 功耗 ≥ 底噪×3 → 在干活（音乐低相是 5.5 倍，留足余量）
    "work_temp_rise": 8.0,     # 或温度 ≥ 底温+8℃ → 在干活
    "cpu_bound_pct": 60.0,     # CPU 忙成这样也算在干活（GPU 侧可能是 CPU 瓶颈）
    "quiet_power_ratio": 1.6,  # 功耗 < 底噪×1.6 视为"没在烧电"
    "quiet_temp_rise": 5.0,
    "dwell_s": 3.0,            # 换状态前候选状态需维持的时间（防抖）
    "interval_busy": 1.0,      # 有活时采样间隔
    "interval_idle": 5.0,      # 空闲时采样间隔（省 CPU；判定不用空闲段的密度）
    "interval_deep": 10.0,     # 空闲超过 deep_after_s 后进一步降频
    "deep_after_s": 600.0,
    "buf_max": 900,            # 15 分钟 @1Hz
    "stall_dump_gap": 600.0,   # 两次卡死现场快照的最小间隔
    "timeout_factor": 1.8,     # 同类任务历史 P90 × 此倍数 → 超时线
    "timeout_min_s": 180.0,    # 超时线不低于这个值（防短任务误报）
    "timeout_min_n": 2,        # 至少攒到这么多次历史才启用超时判定
}

CODE_TEXT = {
    "UNKNOWN": ("无 GPU", "gray"),
    "IDLE": ("空闲", "gray"),
    "LOADING": ("加载中", "blue"),
    "WORKING": ("执行中", "green"),
    "STALLED": ("卡死", "red"),
}

_CREATE_NO_WINDOW = 0x08000000
_lock = threading.Lock()
BUF = deque(maxlen=SM["buf_max"])
_last = {"ts": 0.0, "data": None}


# ============================ 基线 ============================

class Baseline:
    """空闲底温/底功耗与峰值功耗的自学习基线（多因素判定的相对证据来源）。

    绝对阈值只对"本机这类凉卡"成立；高底温设备空闲就 55℃+，
    绝对温度线会把空闲判成干活、把卡死判成正常。所以主证据用相对量：
      · ΔT = 当前温度 - 空闲底温
      · 功耗底噪倍数 = 当前功耗 / 空闲功耗
    """

    STEP = 0.05
    PEAK_DECAY = 0.9999
    SAVE_SECS = 30.0

    def __init__(self, path=None):
        self.path = path
        self.idle_p = None
        self.idle_t = None
        self.peak_p = None
        self.ram_work = None      # 干活时段的常态内存占用（%）
        self.ram_idle = None      # 空闲时段的常态内存占用（%）
        self._last_save = 0.0
        self.load()

    def load(self):
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.idle_p = d.get("idle_power")
            self.idle_t = d.get("idle_temp")
            self.peak_p = d.get("peak_power")
            self.ram_work = d.get("ram_work")
            self.ram_idle = d.get("ram_idle")
        except Exception:
            pass

    def save(self, force=False):
        if not self.path:
            return
        now = time.time()
        if not force and now - self._last_save < self.SAVE_SECS:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"idle_power": self.idle_p, "idle_temp": self.idle_t,
                           "peak_power": self.peak_p, "ram_work": self.ram_work,
                           "ram_idle": self.ram_idle, "updated": now}, f)
            os.replace(tmp, self.path)
            self._last_save = now
        except Exception:
            pass

    def update(self, g, ram_pct=None, busy=None):
        """busy: 队列里是否有任务在跑。True=有（禁止学底噪）/ False=没有 / None=未知。

        ★ 2026-11-01：学空闲底噪必须「队列为空」。旧版只看 util<idle_util 且功耗低，
          实测有 20 条「队列在跑、util=0、功耗≈24W、elapsed≈0s」（模型加载瞬间）
          被当成底噪学进去 → 底噪被拉低 → ΔT 与功耗倍数被放大 → 判定偏向"真干活"。
          busy 为 None（取不到队列）时退回旧行为，避免采集源不可用时基线永久不更新。
        """
        if not g:
            return
        p, t, u = g.get("power"), g.get("temp"), g.get("util")
        if p is not None:
            self.peak_p = p if self.peak_p is None else max(p, self.peak_p * self.PEAK_DECAY)
        # 只在"看着就是空闲"的样本上跟基线，避免把干活时的温度/功耗当底
        idle_cond = (busy is not True
                     and u is not None and u < TH["idle_util"]
                     and p is not None and p < self.idle_power_line())
        if idle_cond:
            self.idle_p = p if self.idle_p is None else self.idle_p + self.STEP * (p - self.idle_p)
            if t is not None:
                self.idle_t = t if self.idle_t is None else self.idle_t + self.STEP * (t - self.idle_t)
        # 内存常态：干活/空闲各学一条（黄线用相对涨幅，红线仍绝对）
        if ram_pct is not None:
            if u is not None and u >= TH["fake_util"]:
                self.ram_work = (ram_pct if self.ram_work is None
                                 else self.ram_work + self.STEP * (ram_pct - self.ram_work))
            elif idle_cond:
                self.ram_idle = (ram_pct if self.ram_idle is None
                                 else self.ram_idle + self.STEP * (ram_pct - self.ram_idle))
        self.save()

    def idle_power_line(self):
        """空闲功耗线 = max(绝对 45W, 学习到的底功耗×1.5)。"""
        base = (self.idle_p or 0.0) * 1.5
        return max(TH["idle_power"], base)

    def floor_power(self):
        """底噪功耗（判定"有没有在烧电"的基准），没学到时用绝对线。"""
        return self.idle_p if self.idle_p is not None else TH["idle_power"]

    def power_frac(self, p):
        if p is None or self.idle_p is None or self.peak_p is None:
            return None
        span = self.peak_p - self.idle_p
        if span < 5.0:          # 区间过小（低功耗卡），退化为绝对判断
            return None
        return (p - self.idle_p) / span

    def temp_rise(self, t):
        if t is None or self.idle_t is None:
            return None
        return t - self.idle_t


BASELINE = Baseline(BASELINE_PATH)


# ============================ 采样 ============================

def _run(cmd, timeout=3):
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = _CREATE_NO_WINDOW
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", **kw)
        return r.stdout or ""
    except Exception:
        return ""


def _num(x):
    try:
        return float(str(x).strip().strip("[]").replace("N/A", ""))
    except (ValueError, AttributeError, TypeError):
        return None


def _gpu():
    q = "name,utilization.gpu,temperature.gpu,power.draw,memory.used,memory.total"
    out = _run(["nvidia-smi", "--query-gpu=" + q, "--format=csv,noheader,nounits"])
    line = next((ln for ln in out.splitlines() if ln.strip()), "")
    if not line or "not a valid field" in line or "Failed" in line:
        return None
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 6:
        return None
    used, total = _num(parts[4]), _num(parts[5])
    return {
        "name": parts[0],
        "util": _num(parts[1]),
        "temp": _num(parts[2]),
        "power": _num(parts[3]),
        "vram_used_gb": None if used is None else round(used / 1024.0, 1),
        "vram_total_gb": None if total is None else round(total / 1024.0, 1),
        "vram_used_mb": used,
    }


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _ram():
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            return {"percent": round(vm.percent, 1),
                    "used_gb": round(vm.used / 1073741824, 1),
                    "total_gb": round(vm.total / 1073741824, 1)}
        except Exception:
            pass
    if os.name == "nt":
        try:
            st = _MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            total = st.ullTotalPhys / 1073741824
            used = (st.ullTotalPhys - st.ullAvailPhys) / 1073741824
            pct = round(100.0 * used / total, 1) if total else None
            return {"percent": pct, "used_gb": round(used, 1), "total_gb": round(total, 1)}
        except Exception:
            return None
    return None


def _cpu():
    if psutil is None:
        return {"percent": None}
    try:
        return {"percent": round(psutil.cpu_percent(interval=None), 1)}
    except Exception:
        return {"percent": None}


def probe_queue():
    """同步读取 ComfyUI 队列（供采样线程用，不需要事件循环）。

    直接读 PromptQueue 的 queue / currently_running 两个容器（execution.py 里的
    公开字段），不走 get_current_queue()——后者会 deepcopy，1Hz 采样下没必要。
    任何一步失败都返回 busy=None，判定侧会退回单点逻辑，不因采集失败而卡死。
    """
    try:
        from server import PromptServer
        pq = getattr(PromptServer.instance, "prompt_queue", None)
        if pq is None:
            return {"busy": None}
        getter = getattr(pq, "get_current_queue_volatile", None)
        if getter is not None:
            running, queued = getter()
        else:
            res = pq.get_current_queue()
            if hasattr(res, "__await__"):       # 万一某版本改成 async
                return {"busy": None}
            running, queued = res
        busy = bool(running or queued)
        pid = sig = label = None
        if running:
            item = running[0]
            pid = item[1] if len(item) > 1 else None
            prompt = item[2] if len(item) > 2 else None
            classes = sorted({v.get("class_type") for v in (prompt or {}).values()
                              if isinstance(v, dict) and v.get("class_type")})
            if classes:
                sig = hashlib.sha1("|".join(classes).encode("utf-8")).hexdigest()[:12]
                key = [c for c in classes
                       if any(k in c for k in ("Sampler", "Generate", "Music", "Video",
                                               "Latent", "Image", "Audio"))]
                label = "+".join((key or classes)[:2])
        return {"busy": busy, "pid": pid, "sig": sig, "label": label,
                "pending": len(queued)}
    except Exception:
        return {"busy": None}


# ============================ 任务耗时台账 ============================

class TaskBook:
    """按工作流签名记历史耗时，用来做「同类任务超时」这一档兜底判据。

    签名 = 该工作流用到的节点类集合的 sha1 前 12 位（自学习，不硬编码分类）。
    只有在同类任务攒够 timeout_min_n 次之后才启用超时判定，避免冷启动误报。
    """

    MAX_KEEP = 40

    def __init__(self, path=None):
        self.path = path
        self.data = {}
        self.load()

    def load(self):
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f) or {}
        except Exception:
            self.data = {}

    def save(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def record(self, sig, label, dur):
        if not sig or not dur or dur <= 0:
            return
        rec = self.data.setdefault(sig, {"label": label, "d": []})
        if label:
            rec["label"] = label
        rec["d"].append(round(dur, 1))
        rec["d"] = rec["d"][-self.MAX_KEEP:]
        self.save()

    def history(self, sig):
        rec = self.data.get(sig) or {}
        return rec.get("d") or [], rec.get("label")

    def timeout(self, sig):
        d, _ = self.history(sig)
        if len(d) < SM["timeout_min_n"]:
            return None
        sd = sorted(d)
        p90 = sd[min(len(sd) - 1, int(len(sd) * 0.9))]
        return max(SM["timeout_min_s"], p90 * SM["timeout_factor"])


TASKBOOK = TaskBook(TASKBOOK_PATH)


# ============================ 窗口工具 ============================

def _win(sec, now=None):
    now = now or time.time()
    lo = now - sec
    with _lock:                 # deque 边迭代边被采样线程 append 会抛 RuntimeError
        rows = list(BUF)
    return [s for s in rows if s["ts"] >= lo]


def _vals(w, key):
    return [s.get(key) for s in w if s.get(key) is not None]


def _med(xs):
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if n == 0:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _rng(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    return max(xs) - min(xs)


# ============================ 状态机 ============================

class StateMachine:
    """四态：IDLE / LOADING / WORKING / STALLED。

    判据优先级（队列有活时）：
      1. 功耗≥底噪×3 或 ΔT≥8 或 CPU≥60 → WORKING（**先判作业**）
         —— 必须先于加载：出图跑到中段显存会反复分配（9~15GB 抖动），功耗却稳在 160W，
            那是分配器抖动，不是加载。只有"功耗没起 + 显存在涨"才是真搬模型。
      2. 功耗没起但显存在涨            → LOADING
      3. 不在烧电 + 所有量 30s 内几乎不变 ≥60s → STALLED（自旋型卡死）
      4. 不在烧电 + 不升温 + 显存不动 ≥60s    → STALLED（阻塞型卡死）
      5. 超同类任务历史 P90×1.8        → STALLED（兜底）
      6. 其余                         → LOADING（刚起步，还没进入稳态）
    队列空 → IDLE（不看窗口，队列是硬信号）。
    """

    def __init__(self):
        self.code = None
        self.since = 0.0
        self.cand = None
        self.cand_since = 0.0
        self.frozen_since = None
        self.quiet_since = None
        self.last_dump = 0.0

    def _settle(self, code, now):
        """候选状态需维持 dwell_s 才真正切换（防抖）。"""
        if code != self.cand:
            self.cand, self.cand_since = code, now
        if self.code is None:
            self.code, self.since = code, now
        elif code != self.code:
            if (now - self.cand_since) >= SM["dwell_s"] and (now - self.since) >= SM["dwell_s"]:
                self.code, self.since = code, now
        else:
            self.cand_since = min(self.cand_since, self.since)
        return self.code

    def step(self, snap, now):
        """snap：一次采样的即时值 + 队列信息 → 返回判定 dict。"""
        g = snap["gpu"]
        busy = snap["busy"]
        b = BASELINE

        if not g:
            return self._emit("UNKNOWN", "取不到 nvidia-smi", now, snap)

        w = _win(SM["win_s"], now)
        fz = _win(SM["freeze_win_s"], now)
        u_med = _med(_vals(w, "util")) if w else g.get("util")
        p_med = _med(_vals(w, "power")) if w else g.get("power")
        t_med = _med(_vals(w, "temp")) if w else g.get("temp")
        c_med = _med(_vals(w, "cpu")) if w else snap.get("cpu")
        v_rng = _rng(_vals(w, "vram")) if w else None
        base_p = b.floor_power()
        base_t = b.idle_t
        d_temp = (t_med - base_t) if (t_med is not None and base_t is not None) else None
        ratio = (p_med / base_p) if (p_med is not None and base_p) else None

        # ---------- 队列空：硬信号，直接空闲 ----------
        if busy is False:
            self.frozen_since = self.quiet_since = None
            why = "队列为空"
            if p_med is not None:
                why += "，功耗 %.0fW（底噪 %.0fW）" % (p_med, base_p)
            return self._emit("IDLE", why, now, snap)

        # ---------- 队列未知：退回单点逻辑，不假装知道窗口 ----------
        if busy is None:
            self.frozen_since = self.quiet_since = None
            return self._emit_instant(g, now, snap)

        # ---------- 队列有活 ----------
        # 1) 显存在涨 = 正在加载
        vram_rising = v_rng is not None and v_rng >= SM["vram_rise_mb"]

        # 2) 是否在烧电（★ 必须先算，且优先级高于"静止"）
        working = ((ratio is not None and ratio >= SM["work_power_ratio"])
                   or (d_temp is not None and d_temp >= SM["work_temp_rise"])
                   or (c_med is not None and c_med >= SM["cpu_bound_pct"]))
        quiet = ((ratio is not None and ratio < SM["quiet_power_ratio"])
                 and (d_temp is None or d_temp < SM["quiet_temp_rise"])
                 and not vram_rising)
        if quiet:
            self.quiet_since = self.quiet_since or now
        else:
            self.quiet_since = None
        quiet_s = (now - self.quiet_since) if self.quiet_since else 0.0

        # 3) 冻结检测（自旋型卡死）。★ 只能在"不在烧电"时才成立——
        #    music3 高相功耗稳定在 180W、利用率 99% 一动不动，那是它本来就稳，
        #    不是卡死。实测若不排除，10 首音乐会报 379 条假卡死。
        frozen, fr = self._frozen(fz, now) if not working else (False, 0.0)

        bits = []
        if ratio is not None:
            bits.append("功耗 %.0fW(底噪×%.1f)" % (p_med, ratio))
        if d_temp is not None:
            bits.append("离底温 %+.0f℃" % d_temp)
        if v_rng is not None:
            bits.append("显存波动 %.0fMB" % v_rng)
        info = "、".join(bits)

        # 超时线：同类任务历史 P90 × 1.8（至少 180s，且攒够 2 次才启用）
        to = TASKBOOK.timeout(snap.get("sig"))
        el = snap.get("elapsed") or 0.0
        over = bool(to and el > to)

        if working:
            # ★ 先判作业再判加载：实测出图跑到 63s 时显存在 9.3~15.4GB 之间反复分配
            #   （分配器抖动 / VAE 分块），功耗却稳稳 160W——那是在干活，不是加载。
            #   只有"功耗没上来、显存在涨"才是真的从磁盘搬模型。
            if over:
                # 还在算电、但已跑过同类历史的 1.8 倍：这不是卡死，是异常拖长。
                # 不伪装成 STALLED（那会冤枉它），但必须显眼——标黄 + 文案点明。
                out = self._emit("WORKING", "已跑 %.0fs，超过同类历史线 %.0fs，但仍在算电：%s"
                                 % (el, to, info), now, snap)
                out["text"] = "执行中（超时）"
                out["color"] = "yellow"
                out["warn"] = True
                return out
            return self._emit("WORKING", info, now, snap)
        if vram_rising:
            return self._emit("LOADING", "功耗未起但显存在涨（搬模型中）：" + info, now, snap)
        if frozen and fr >= SM["stall_after_s"]:
            self._dump_stall(snap, now)
            return self._emit("STALLED", "队列有活但 %ss 内所有指标纹丝不动、且不在烧电：%s"
                              % ("%.0f" % fr, info), now, snap, frozen=fr)
        if quiet_s >= SM["stall_after_s"]:
            self._dump_stall(snap, now)
            return self._emit("STALLED", "队列有活但已 %.0fs 不烧电不升温、显存不动：%s"
                              % (quiet_s, info), now, snap)
        if to and el > to:
            self._dump_stall(snap, now)
            return self._emit("STALLED", "已跑 %.0fs，超过同类任务历史线 %.0fs：%s"
                              % (el, to, info), now, snap)
        if frozen:
            return self._emit("LOADING", "指标静止但功耗已低于作业线（%.0fs，判死线 %g 秒）：%s"
                              % (fr, SM["stall_after_s"], info), now, snap, frozen=fr)
        return self._emit("LOADING", "刚起步/过渡（%s）" % info, now, snap)

    def _frozen(self, fz, now):
        """窗口内所有量几乎不变 → 返回 (True, 已静止秒数)。"""
        if len(fz) < 6:
            self.frozen_since = None
            return False, 0.0
        span = fz[-1]["ts"] - fz[0]["ts"]
        if span < SM["freeze_min_span"]:
            self.frozen_since = None
            return False, 0.0
        ok = True
        for key, eps in (("util", SM["eps_util"]), ("power", SM["eps_power"]),
                         ("temp", SM["eps_temp"]), ("vram", SM["eps_vram"])):
            r = _rng(_vals(fz, key))
            if _vals(fz, key) and (r is None or r > eps):
                ok = False
                break
        if not ok:
            self.frozen_since = None
            return False, 0.0
        if self.frozen_since is None:
            self.frozen_since = fz[0]["ts"]      # 从"窗口起点"算起，静止是累积的
        return True, now - self.frozen_since

    def _emit(self, code, why, now, snap, **extra):
        code = self._settle(code, now)
        text, color = CODE_TEXT.get(code, ("?", "gray"))
        out = {"code": code, "text": text, "color": color, "why": why,
               "since": round(now - self.since, 1)}
        out.update(extra)
        snap["state"] = out
        return out

    def _emit_instant(self, g, now, snap):
        """队列取不到：用单点阈值兜底（不声称窗口判定）。"""
        u, p, t = g.get("util"), g.get("power"), g.get("temp")
        b = BASELINE
        line = b.idle_power_line()
        if u is not None and u < TH["idle_util"] and (p is None or p < line):
            code, why = "IDLE", "队列未知，单点兜底：util<%g%% 且 功耗<%gW" % (TH["idle_util"], line)
        elif u is not None and u >= TH["fake_util"]:
            code, why = "WORKING", "队列未知，单点兜底：util ≥%g%%" % TH["fake_util"]
        else:
            code, why = "LOADING", "队列未知，单点兜底（功耗 %s / 温度 %s）" % (p, t)
        return self._emit(code, why, now, snap)

    def _dump_stall(self, snap, now):
        """判死时把前后样本落盘，供以后拿真样本标定（本机还没真卡死过）。"""
        if now - self.last_dump < SM["stall_dump_gap"]:
            return
        self.last_dump = now
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            fn = os.path.join(DATA_DIR, "stall_%s.json" % time.strftime("%Y%m%d-%H%M%S"))
            with _lock:
                rows = list(BUF)[-180:]
            with open(fn, "w", encoding="utf-8") as f:
                json.dump({"note": "判定卡死时的现场快照，用于以后标定判据",
                           "thresholds": TH, "sm": SM,
                           "baseline": {"idle_power": BASELINE.idle_p,
                                        "idle_temp": BASELINE.idle_t,
                                        "peak_power": BASELINE.peak_p},
                           "current": snap, "samples": rows},
                          f, ensure_ascii=False, indent=1)
        except Exception:
            pass


SMACHINE = StateMachine()


# ============================ 颜色 ============================

def evidence(g, b):
    """多因素证据 → (power_ev, temp_ev, frac, dtemp)。"""
    if not g:
        return False, False, None, None
    p, t, u = g.get("power"), g.get("temp"), g.get("util")
    frac = b.power_frac(p) if b is not None else None
    dtemp = b.temp_rise(t) if b is not None else None
    power_ev = ((frac is not None and frac >= TH["rise_power_frac"])
                or (p is not None and p >= TH["real_power"]))
    temp_ev = ((dtemp is not None and dtemp >= TH["rise_temp"])
               or (dtemp is None and t is not None and t >= TH["real_temp"]
                   and (u is None or u >= TH["temp_abs_min_util"])))
    return power_ev, temp_ev, frac, dtemp


def _colors(g, ram, cpu, b, code=None):
    """指标配色（服务端算好，前端只映射颜色名）。等级分档 + 多因素证据。

    code：当前状态码。只有 STALLED 才把功耗/利用率标红——
    队列在跑、功耗低只是"这类负载省电"，不是异常。
    """
    out = {"power": None, "util": None, "temp": None, "ram": None, "cpu": None}
    if g:
        u, p, t = g.get("util"), g.get("power"), g.get("temp")
        power_ev, _temp_ev, _frac, _dt = evidence(g, b)
        stalled = (code == "STALLED")
        if stalled:
            out["power"] = "red"
        elif p is not None:
            out["power"] = ("green" if power_ev else
                            ("gray" if p < b.idle_power_line() else "yellow"))
        if u is not None:
            if stalled:
                out["util"] = "red"
            elif u >= TH["fake_util"]:
                out["util"] = "green" if power_ev else "yellow"
            elif u >= 50:
                out["util"] = "yellow"
            elif u >= TH["idle_util"]:
                out["util"] = "green"
            else:
                out["util"] = "gray"
        if t is not None:
            dt = b.temp_rise(t) if b is not None else None
            out["temp"] = ("red" if t >= TH["alert_temp"]
                           else ("yellow" if (t >= TH["alert_temp"] - 8
                                              or (dt is not None and dt >= 18)) else "green"))
    if ram and ram.get("percent") is not None:
        v = ram["percent"]
        # 红线绝对（OOM 是物理风险）；黄线自适应（比"干活常态"多 12 个百分点也算异常）
        out["ram"] = ("red" if v >= 90 else
                      ("yellow" if (v >= 75 or (b is not None and b.ram_work is not None
                                                and v - b.ram_work >= 12)) else "green"))
    if cpu and cpu.get("percent") is not None:
        v = cpu["percent"]
        out["cpu"] = ("red" if v >= 85 else ("yellow" if v >= 50
                                             else ("green" if v >= 15 else "gray")))
    return out


# ============================ 采样 + 主循环 ============================

_cur_task = {"pid": None, "sig": None, "label": None, "start": None}


def sample_once(busy_override=None):
    """采一次、入缓冲、跑状态机、返回完整载荷。"""
    now = time.time()
    g = _gpu()
    ram = _ram()
    cpu = _cpu()
    q = probe_queue()
    busy = q.get("busy") if busy_override is None else busy_override
    pid = q.get("pid")
    sig = q.get("sig")
    label = q.get("label")

    # 任务计时（pid 变了就是新任务；busy 由真变假就结算）
    elapsed = None
    if busy:
        if pid and pid != _cur_task["pid"]:
            _cur_task.update({"pid": pid, "sig": sig, "label": label, "start": now})
        if _cur_task["start"] is None:
            _cur_task["start"] = now
        elapsed = now - _cur_task["start"]
    else:
        if _cur_task["start"] is not None:
            dur = now - _cur_task["start"]
            TASKBOOK.record(_cur_task["sig"], _cur_task["label"], dur)
            _cur_task.update({"pid": None, "sig": None, "label": None, "start": None})

    snap = {
        "ts": now,
        "gpu": g,
        "cpu": (cpu or {}).get("percent"),
        "ram": (ram or {}).get("percent"),
        "busy": busy,
        "pid": pid or _cur_task["pid"],
        "sig": sig or _cur_task["sig"],
        "elapsed": elapsed,
        "vram": (g or {}).get("vram_used_mb"),
        "util": (g or {}).get("util"),
        "power": (g or {}).get("power"),
        "temp": (g or {}).get("temp"),
    }
    BASELINE.update(g, (ram or {}).get("percent"), busy=busy)

    with _lock:
        BUF.append({k: snap[k] for k in
                    ("ts", "util", "power", "temp", "vram", "cpu", "busy", "pid")})

    state = SMACHINE.step(dict(snap), now)
    g2 = g and dict(g)
    if g2:
        g2.pop("vram_used_mb", None)

    sig_now = snap["sig"]
    hist, hlabel = TASKBOOK.history(sig_now) if sig_now else ([], None)
    prev_i = _state_interval(state["code"])

    payload = {
        "ts": int(now * 1000),
        "gpu": g2,
        "ram": ram,
        "cpu": cpu,
        "busy": busy,
        "verdict": state,          # 前端兼容：仍读 verdict.code/text/color/why
        "state": {"code": state["code"], "text": state["text"],
                  "since": state.get("since"), "frozen": state.get("frozen")},
        "colors": _colors(g, ram, cpu, BASELINE, state["code"]),
        "window": {"sec": SM["win_s"],
                   "util": _med(_vals(_win(SM["win_s"], now), "util")),
                   "power": _med(_vals(_win(SM["win_s"], now), "power")),
                   "temp": _med(_vals(_win(SM["win_s"], now), "temp")),
                   "n": len(_win(SM["win_s"], now))},
        "task": {"pid": snap["pid"], "label": label or hlabel, "sig": sig_now,
                 "elapsed": None if elapsed is None else round(elapsed, 1),
                 "timeout": TASKBOOK.timeout(sig_now) if sig_now else None,
                 "hist_n": len(hist)},
        "sampler": {"alive": _thread is not None and _thread.is_alive(),
                    "interval": prev_i, "buf": len(BUF)},
        "baseline": {"idle_power": BASELINE.idle_p,
                     "idle_temp": BASELINE.idle_t,
                     "peak_power": BASELINE.peak_p,
                     "ram_work": BASELINE.ram_work,
                     "ram_idle": BASELINE.ram_idle},
        "thresholds": TH,
    }
    return payload


def _state_interval(code):
    if code in ("LOADING", "WORKING", "STALLED"):
        return SM["interval_busy"]
    idle_s = time.time() - (SMACHINE.since or time.time())
    if idle_s >= SM["deep_after_s"]:
        return SM["interval_deep"]
    return SM["interval_idle"]


def collect(busy=None):
    """给 HTTP 层用：优先返回常驻线程的最新结果。

    线程活着时**不再现场采样**——否则 HTTP 请求会和采样线程同时往缓冲里塞点，
    窗口里的时间分布就不均匀了（判定要靠时间加权，密度不能乱）。
    """
    now = time.time()
    with _lock:
        data = _last["data"]
        ts = _last["ts"]
    alive = _thread is not None and _thread.is_alive()
    if data is not None and (alive or now - ts < TTL):
        return data
    data = sample_once(busy)
    with _lock:
        _last["ts"], _last["data"] = now, data
    return data


# ============================ 常驻采样线程 ============================

_thread = None
_stop = threading.Event()


def _loop():
    """常驻采样：忙时 1s、空闲 5s、长时间空闲 10s。

    为什么要常驻：节点原来是"前端来问才采一次"，面板一关就没有样本，
    窗口判定会一直是空的。判定必须由服务端自己驱动，不能由浏览器驱动。
    """
    while not _stop.is_set():
        try:
            interval = SM["interval_idle"]
            data = sample_once()
            interval = _state_interval((data.get("state") or {}).get("code"))
            with _lock:
                _last["ts"], _last["data"] = time.time(), data
        except Exception:
            interval = SM["interval_idle"]
        _stop.wait(interval)


def start_sampler():
    """幂等启动（ComfyUI 重载模块时不会起第二个线程）。"""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="HwmonHUDSampler", daemon=True)
        _thread.start()
    return True


def stop_sampler():
    _stop.set()


# 预热：psutil 首次 cpu_percent 返回 0.0
if psutil is not None:
    try:
        psutil.cpu_percent(interval=None)
    except Exception:
        pass
