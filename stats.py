# -*- coding: utf-8 -*-
"""HwmonHUD 数据采集层（独立实现，不依赖任何第三方监控节点）。

指标（五项）：GPU 利用率 / GPU 温度 / GPU 功耗 / 内存利用率 / CPU 负载
判定：多因素综合（util + 功耗离基线 + 温度离基线），绝对线只做兜底；
      配色也在服务端算好（前端只负责画），避免两处阈值各自漂移。

与 tools/hwmon/hwmon.py 的关系：阈值、判定、配色三套逻辑保持一致，改一边必须同步另一边。
"""
import ctypes
import json
import os
import subprocess
import threading
import time

try:
    import psutil
except Exception:          # 仅在极简环境触发
    psutil = None

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE_PATH = os.path.join(HERE, "_data", "baseline.json")

TTL = 1.0                  # 秒；前端 1.5s 轮询，后端再缓存一层

# 与 tools/hwmon/hwmon.py 的 CFG 同步（改一边必须改另一边）
TH = {
    "idle_util": 15.0, "idle_power": 60.0,
    "fake_util": 85.0,
    "real_power": 110.0, "real_temp": 56.0,
    "alert_temp": 83.0,
    "rise_temp": 12.0,         # 温度高于空闲基线 ≥ 此值 → 温度侧证据
    "rise_power_frac": 0.55,   # 功耗在(基线→峰值)区间的位置 ≥ 此比例 → 功耗侧证据
    "temp_abs_min_util": 50.0, # 绝对温度兜底时 util 至少这么高
}

_CREATE_NO_WINDOW = 0x08000000
_lock = threading.Lock()
_cache = {"ts": 0.0, "data": None}


class Baseline:
    """空闲底温/底功耗与峰值功耗的自学习基线（多因素判定的相对证据来源）。

    绝对阈值（56℃/110W）只对"本机这类凉卡"成立；高底温设备空闲就 55℃+，
    绝对温度线会把空闲判成干活、把卡死判成正常。所以主证据改相对量：
      · ΔT = 当前温度 - 空闲底温
      · 功耗区间比 = (当前功耗 - 空闲功耗) / (峰值功耗 - 空闲功耗)
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

    def update(self, g, ram_pct=None):
        if not g:
            return
        p, t, u = g.get("power"), g.get("temp"), g.get("util")
        if p is not None:
            self.peak_p = p if self.peak_p is None else max(p, self.peak_p * self.PEAK_DECAY)
        # 只在"看着就是空闲"的样本上跟基线，避免把干活时的温度/功耗当底
        idle_cond = (u is not None and u < TH["idle_util"]
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
        """空闲功耗线 = max(绝对 60W, 学习到的底功耗×1.5)；高功耗卡空转也可能 70W+。"""
        base = (self.idle_p or 0.0) * 1.5
        return max(TH["idle_power"], base)

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


def evidence(g, b):
    """多因素证据 → (power_ev, temp_ev, frac, dtemp)。"""
    if not g:
        return False, False, None, None
    p, t, u = g.get("power"), g.get("temp"), g.get("util")
    frac = b.power_frac(p) if b is not None else None
    dtemp = b.temp_rise(t) if b is not None else None
    power_ev = ((frac is not None and frac >= TH["rise_power_frac"])
                or (p is not None and p >= TH["real_power"]))
    # 绝对温度只在"还没学好基线"时兜底：高底温设备的空闲温度本身就高
    temp_ev = ((dtemp is not None and dtemp >= TH["rise_temp"])
               or (dtemp is None and t is not None and t >= TH["real_temp"]
                   and (u is None or u >= TH["temp_abs_min_util"])))
    return power_ev, temp_ev, frac, dtemp


def _verdict(g, b):
    """多因素判定：空闲 / 假忙卡死 / 真·生产驴 / 低负载。与服务端 hwmon 同源。"""
    if not g:
        return {"code": "UNKNOWN", "text": "无 GPU", "why": "n/a", "color": "gray"}
    u, p, t = g.get("util"), g.get("power"), g.get("temp")
    idle_line = b.idle_power_line() if b is not None else TH["idle_power"]
    if u is not None and u < TH["idle_util"] and (p is None or p < idle_line):
        return {"code": "IDLE", "text": "空闲", "color": "gray",
                "why": "util <%g%% 且 功耗 <%gW" % (TH["idle_util"], idle_line)}
    power_ev, temp_ev, frac, dtemp = evidence(g, b)
    bits = []
    if frac is not None:
        bits.append("功耗区间比 %.2f" % frac)
    if dtemp is not None:
        bits.append("离基线 %+.0f℃" % dtemp)
    why = "、".join(bits) or "无基线数据（按绝对阈值兜底）"
    if u is not None and u >= TH["fake_util"]:
        if not power_ev and not temp_ev:
            return {"code": "FAKE", "text": "假忙卡死", "color": "red",
                    "why": "util ≥%g%% 但 功耗/温度都没离基线：%s" % (TH["fake_util"], why)}
        return {"code": "REAL", "text": "真·生产驴", "color": "green",
                "why": "util ≥%g%% 且 有能耗证据：%s" % (TH["fake_util"], why)}
    if power_ev or temp_ev:
        return {"code": "REAL", "text": "真·生产驴", "color": "green",
                "why": "有能耗证据：%s" % why}
    return {"code": "LOW", "text": "低负载/加载中", "color": "yellow",
            "why": "功耗与温度均未明显离基线：%s" % why}


def _colors(g, ram, cpu, b):
    """指标配色（服务端算好，前端只映射颜色名）。等级分档 + 多因素证据。"""
    out = {"power": None, "util": None, "temp": None, "ram": None, "cpu": None}
    if g:
        u, p, t = g.get("util"), g.get("power"), g.get("temp")
        power_ev, temp_ev, _frac, _dt = evidence(g, b)
        fake = (u is not None and u >= TH["fake_util"] and not power_ev and not temp_ev)
        if fake:
            out["power"] = "red"
        elif p is not None:
            out["power"] = ("green" if power_ev else
                            ("gray" if p < b.idle_power_line() else "yellow"))
        if u is not None:
            if u >= TH["fake_util"]:
                out["util"] = "green" if power_ev else ("red" if fake else "yellow")
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


def _collect_uncached():
    g = _gpu()
    ram = _ram()
    cpu = _cpu()
    BASELINE.update(g, (ram or {}).get("percent"))
    return {"ts": int(time.time() * 1000),
            "gpu": g,
            "ram": ram,
            "cpu": cpu,
            "verdict": _verdict(g, BASELINE),
            "colors": _colors(g, ram, cpu, BASELINE),
            "baseline": {"idle_power": BASELINE.idle_p,
                         "idle_temp": BASELINE.idle_t,
                         "peak_power": BASELINE.peak_p,
                         "ram_work": BASELINE.ram_work,
                         "ram_idle": BASELINE.ram_idle},
            "thresholds": TH}


def collect():
    now = time.time()
    with _lock:
        if _cache["data"] is not None and now - _cache["ts"] < TTL:
            return _cache["data"]
        data = _collect_uncached()
        _cache["ts"], _cache["data"] = now, data
        return data


# 预热：psutil 首次 cpu_percent 返回 0.0
if psutil is not None:
    try:
        psutil.cpu_percent(interval=None)
    except Exception:
        pass
