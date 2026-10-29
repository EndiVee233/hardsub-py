#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hardsub.py — 智能硬编码(烧录)字幕 · 命令行版

用途：把 .ass/.ssa/.srt/.vtt 字幕永久烧进视频画面（硬字幕），
      自动挑选 NVIDIA / AMD / Intel 硬件编码，不可用时自动回退 CPU 软编码。

相比旧的 Smart_Subtitle_Encode.bat：
  1. 纯 Python，不依赖 cmd 的延迟展开 / 代码页 / chcp：中文路径、感叹号、括号、
     空格全都不用再躲（旧脚本要求路径和文件名里不能有 !）。
  2. 硬件编码器是"实测"出来的：真的编 3 帧验证驱动能不能跑，而不是只看
     `ffmpeg -encoders` 列表；正式压制前还会用真实视频试跑 0.5 秒整条滤镜链，
     跑不通就自动回退 CPU，避免白等一小时才发现失败。
  3. 修复"第一条字幕被吞"的 bug（见 inject_sacrificial_event 的注释）。
  4. 保留全部音轨（旧脚本 -acodec copy 只会留一条），音轨与封装不兼容时自动转 AAC。
  5. 进度条 + 压制后自动校验（时长 / 分辨率 / 音轨数 / HDR 元数据）。
  6. 全参数化命令行，可被其它脚本调度。
  7. HDR（HDR10 / HLG）全程 10bit，写回 BT.2020 + PQ/HLG 标签，保留 HDR10 静态元数据；
     硬件链路的 hwdownload/hwupload 按位深用 p010le，不再因为写死 nv12 而整条失败。
     HDR 是静默自动识别的：HDR 源走 HDR 链路，普通源走 SDR 链路，不用加参数或做选择。
     字幕颜色会按 BT.2408 图形白 203nit 预映射到 PQ/HLG（SDR 白在 PQ 里原本≈9900nit，
     会白得刺眼），彩色内联覆盖也一起处理。--tonemap 仍可把 HDR 转成 SDR（BT.709 8bit）。
  8. 进度条不会再被 ffmpeg 的警告卡住：stdout/stderr 各一个读取线程，进度字段三级回退，
     结束时汇总警告、失败时打印真正的错误行。
  9. 逐词高亮（Default 样式里 {\\c&H...&}word{\\c...} 这种写法）会把亮起词的主色和
     描边色按色相归一到 1000nit（HDR 峰值），不管这个单词是白、橙、青还是紫都能
     顶满亮度；普通文字和其他样式仍按 203nit 图形白映射。

用法示例：
  hardsub.exe                                    交互式，扫描当前目录
  hardsub.exe --probe                            环境自检（推荐先跑一次）
  hardsub.exe --list                             列出当前目录的视频 / 字幕
  hardsub.exe -v movie.mkv -s movie.ass -a nvidia -c h265 -q 20 -y
  hardsub.exe -v movie.mp4 -s zh.ass -d "D:\\影片" -o 输出.mp4
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "1.3.0"

VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".flv", ".webm", ".avi", ".m4v", ".ts",
              ".wmv", ".mpg", ".mpeg", ".rmvb")
SUB_EXTS = (".ass", ".ssa", ".srt", ".vtt")

CODEC_LABEL = {"h264": "H.264 (AVC)", "h265": "H.265 (HEVC)", "av1": "AV1"}
DEFAULT_QUALITY = {"h264": 23, "h265": 23, "av1": 30}   # AV1 的 crf 刻度偏大
QUALITY_RANGE = {"h264": (0, 51), "h265": (0, 51), "av1": (0, 63)}

CPU_ENCODERS = {
    "h264": ["libx264"],
    "h265": ["libx265"],
    "av1": ["libsvtav1", "libaom-av1"],
}
HW_ENCODERS = {
    "nvidia": {"h264": "h264_nvenc", "h265": "hevc_nvenc", "av1": "av1_nvenc"},
    "amd": {"h264": "h264_amf", "h265": "hevc_amf", "av1": "av1_amf"},
    "intel": {"h264": "h264_qsv", "h265": "hevc_qsv", "av1": "av1_qsv"},
}
VENDOR_LABEL = {"nvidia": "NVIDIA NVENC", "amd": "AMD AMF", "intel": "Intel QSV",
                "cpu": "CPU 软编码"}

# 这些音频编码无法放进 mp4/mov 时需要转码
MP4_OK_AUDIO = {"aac", "mp3", "ac2", "ac3", "eac3", "alac", "mp2", "opus", "dts"}

# HDR 的传递特性 -> 给人看的名字。HDR10 用 PQ，HLG 是广电那套。
HDR_TRANSFERS = {"smpte2084": "HDR10（PQ）", "arib-std-b67": "HLG"}
UNKNOWN_TAGS = {"", "unknown", "unspecified", "reserved", "n/a", "none"}

# --------------------------------------------------------------------------
# HDR 字幕配色
# --------------------------------------------------------------------------
# 问题：字幕是 SDR 的（BT.709，100% 白 = 100nit）。把这种白色直接烧进 PQ 视频，
# 235 会被当成 PQ 的 0.92 信号 —— 实测白字 10bit 939（约 9900nit），在黑场景里
# 白得刺眼，边缘还会发灰（实际是 HDR 过曝）。
# 做法（最常见的"字幕颜色预映射"）：按 BT.2408 的图形白 203nit，把每个 SDR 颜色
# 经线性光 BT.709 -> BT.2020 变换后，用目标传递特性重新编码成码值写回字幕：
#   PQ  : 白 ≈ 148/255（0.58 信号 ≈ 203nit）
#   HLG : 白 ≈ 191/255（0.75 信号，1000nit 屏上 ≈ 203nit）
# 只改颜色（含 \c / \1c..\4c 内联覆盖），字号、位置、透明度、渐变时序都不动。
# 这样 HDR 片源正常走 10bit HDR 链路，字幕亮度也和 SDR 下看起来一致。
HDR_SUB_WHITE_NITS = 203.0       # BT.2408 图形白（普通字幕）
HDR_HIGHLIGHT_NITS = 1000.0      # 逐词高亮的目标亮度（HDR10 常见峰值）
HDR_HLG_PEAK_NITS = 1000.0       # HLG 参考显示峰值
HDR_HLG_GAMMA = 1.2              # BT.2100 系统 gamma

# BT.709 -> BT.2020 线性光矩阵（D65 白点，行和 = 1，白色亮度不变）
BT709_TO_BT2020 = (
    (0.627403895934699, 0.329283038377883, 0.043313065687418),
    (0.069097289358232, 0.919540395075459, 0.011362315566309),
    (0.016391438875150, 0.088013307877226, 0.895595253247624),
)

# SMPTE ST 2084（PQ）常数
PQ_M1, PQ_M2 = 0.1593017578125, 78.84375
PQ_C1, PQ_C2, PQ_C3 = 0.8359375, 18.8515625, 18.6875

# BT.2100 HLG OETF 常数
HLG_A, HLG_B, HLG_C = 0.17883277, 0.28466892, 0.55991073


# ==========================================================================
# 终端与日志
# ==========================================================================
class C:
    _on = sys.stdout.isatty()
    R = "\033[0m" if _on else ""
    B = "\033[1m" if _on else ""
    G = "\033[32m" if _on else ""
    Y = "\033[33m" if _on else ""
    E = "\033[31m" if _on else ""
    CY = "\033[36m" if _on else ""


def setup_console() -> None:
    """
    Windows 控制台切到 UTF-8，避免中文乱码（旧 bat 用 chcp 936 的老办法）。
    打包成 exe 后，输出被重定向/被别的程序捕获时也必须是 UTF-8，否则对方拿到的是乱码。
    """
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleOutputCP(65001)
            k.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001
            pass
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        rc = getattr(stream, "reconfigure", None)
        if callable(rc):
            try:
                rc(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


setup_console()   # 模块级就切好，连早退出的报错信息也是 UTF-8


def _emit(text: str, stream) -> None:
    """stdout/stderr 可能为 None（无控制台场景），写失败也不能崩。"""
    if stream is None:
        return
    try:
        print(text, file=stream)
    except (ValueError, OSError, UnicodeError):
        pass


def log(msg: str = "") -> None:
    _emit(msg, sys.stdout)


def info(msg: str) -> None:
    _emit(f"{C.CY}[信息]{C.R} {msg}", sys.stdout)


def ok(msg: str) -> None:
    _emit(f"{C.G}[完成]{C.R} {msg}", sys.stdout)


def warn(msg: str) -> None:
    _emit(f"{C.Y}[警告]{C.R} {msg}", sys.stdout)


def err(msg: str) -> None:
    _emit(f"{C.E}[错误]{C.R} {msg}", sys.stderr)


class SafeArgumentParser(argparse.ArgumentParser):
    """无 stderr 的环境下（计划任务、隐藏控制台）argparse 原生实现会抛异常。"""

    def _print_message(self, message, file=None):  # type: ignore[override]
        if message:
            _emit(message, file or sys.stderr)

    def exit(self, status: int = 0, message: str | None = None):  # type: ignore[override]
        if message:
            self._print_message(message, sys.stderr)
        raise SystemExit(status)


# ==========================================================================
# 外部工具
# ==========================================================================
def app_dir() -> Path:
    """程序所在目录（打包成 exe 后即 exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_tool(name: str) -> str | None:
    """顺序：程序同目录 -> 同目录 bin -> 当前目录 -> 常见安装位置 -> PATH。"""
    exe = name + (".exe" if os.name == "nt" else "")
    here = app_dir()
    candidates = [here / exe, here / "bin" / exe, Path.cwd() / exe]
    for p in (r"C:\ffmpeg\bin", r"D:\ffmpeg\bin", r"D:\Program Files\ffmpeg\bin",
              r"C:\Program Files\ffmpeg\bin", "/usr/bin", "/usr/local/bin"):
        candidates.append(Path(p) / exe)
    which = shutil.which(name)
    if which:
        candidates.append(Path(which))
    for c in candidates:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            continue
    return None


def run_capture(cmd: list[str]) -> subprocess.CompletedProcess:
    """执行命令并捕获输出；命令不存在时返回 127 而不是抛异常（新系统没有 wmic 等）。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except (OSError, ValueError) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def ffmpeg_version(ffmpeg: str) -> str:
    try:
        out = run_capture([ffmpeg, "-version"]).stdout or ""
        m = re.search(r"ffmpeg version (\S+)", out)
        return m.group(1) if m else "未知"
    except Exception:  # noqa: BLE001
        return "未知"


_ENC_LIST: set[str] | None = None


def list_encoders(ffmpeg: str) -> set[str]:
    """一次性拿到本 ffmpeg 编译进去的所有编码器名（便宜的前置筛选）。"""
    global _ENC_LIST
    if _ENC_LIST is None:
        out = run_capture([ffmpeg, "-hide_banner", "-encoders"]).stdout or ""
        names = set()
        for line in out.splitlines():
            m = re.match(r"\s*[A-Z.]{6}\s+(\S+)\s", line)
            if m:
                names.add(m.group(1))
        _ENC_LIST = names
    return _ENC_LIST


_FILTER_LIST: set[str] | None = None


def list_filters(ffmpeg: str) -> set[str]:
    """本 ffmpeg 里有哪些滤镜（判断能不能做 HDR→SDR 色调映射）。"""
    global _FILTER_LIST
    if _FILTER_LIST is None:
        out = run_capture([ffmpeg, "-hide_banner", "-filters"]).stdout or ""
        names = set()
        for line in out.splitlines():
            # 形如 " .. libplacebo   N->V ..." / " TS aap   AA->A ..."（前导空格 + 标记位）
            m = re.match(r"^\s*[A-Z.]{2,3}\s+(\S+)\s", line)
            if m:
                names.add(m.group(1))
        _FILTER_LIST = names
    return _FILTER_LIST


# ==========================================================================
# 媒体信息
# ==========================================================================
@dataclass
class Media:
    path: Path
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    v_index: int = 0
    audio: list[str] = field(default_factory=list)
    # ---- 色彩信息（HDR 判定靠这几个字段） ----
    pix_fmt: str = ""
    depth: int = 8
    color_primaries: str = ""
    color_transfer: str = ""
    color_space: str = ""
    color_range: str = ""
    hdr: str = ""                  # "" / "HDR10（PQ）" / "HLG"
    dovi: bool = False             # 杜比视界（重编码会丢 DV 层，这里只提示）
    master_display: str = ""       # 已经转成 x265 -x265-params 的写法
    max_cll: str = ""              # "MaxCLL,MaxFALL"


def pix_fmt_depth(pix_fmt: str) -> int:
    """
    从像素格式名猜位深：yuv420p10le / p010le -> 10，yuv420p / nv12 -> 8。
    位深一定跟在 'p'（或 gray）后面；nv12 / nv21 结尾的 12、21 是采样方式，不能当位深。
    """
    m = re.search(r"(?:p[0-9]?|gray)(9|10|12|14|16)(?:le|be)?$", (pix_fmt or "").strip().lower())
    return int(m.group(1)) if m else 8


def clean_tag(value) -> str:
    """ffprobe 会给 'unknown' / 'reserved'，统一当成空。"""
    text = str(value or "").strip()
    return "" if text.lower() in UNKNOWN_TAGS else text


def parse_fps(text: str) -> float:
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(text)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe_ratio(value) -> float | None:
    """ffprobe 的 '34000/50000' 这种分数转成 float。"""
    try:
        text = str(value)
        if "/" in text:
            num, den = text.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else None
        return float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def master_display_param(side: dict) -> str:
    """
    把 ffprobe 的 Mastering display metadata 转成 x265 的 master-display 写法。
    x265 的坐标单位是 0.00002（×50000），亮度单位是 0.0001 cd/m²（×10000）。
    """
    coords = (("G", "green_x", "green_y"), ("B", "blue_x", "blue_y"),
              ("R", "red_x", "red_y"), ("WP", "white_point_x", "white_point_y"))
    parts = []
    for name, kx, ky in coords:
        x, y = probe_ratio(side.get(kx)), probe_ratio(side.get(ky))
        if x is None or y is None:
            return ""
        parts.append(f"{name}({round(x * 50000)},{round(y * 50000)})")
    peak, low = probe_ratio(side.get("max_luminance")), probe_ratio(side.get("min_luminance"))
    if peak is None or low is None:
        return ""
    parts.append(f"L({round(peak * 10000)},{round(low * 10000)})")
    return "".join(parts)


def ffprobe_hdr_metadata(ffprobe: str, path: Path) -> tuple[str, str, bool]:
    """
    解第一帧拿 HDR10 静态元数据（Mastering display / MaxCLL）。
    这些信息 ffprobe 的 -show_streams 里没有，只挂在帧的 side data 上。
    返回 (master-display, "MaxCLL,MaxFALL", 是否 Dolby Vision)。
    """
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0",
           "-read_intervals", "%+#1", "-show_frames",
           "-show_entries", "frame=side_data_list", "-print_format", "json", str(path)]
    res = run_capture(cmd)
    master_display = max_cll = ""
    dovi = False
    if res.returncode != 0:
        return master_display, max_cll, dovi
    try:
        frames = json.loads(res.stdout or "{}").get("frames", [])
    except json.JSONDecodeError:
        return master_display, max_cll, dovi
    for frame in frames:
        for side in frame.get("side_data_list") or []:
            kind = str(side.get("side_data_type") or "")
            if kind.startswith("Mastering display"):
                master_display = master_display or master_display_param(side)
            elif kind.startswith("Content light level"):
                mc, ma = side.get("max_content"), side.get("max_average")
                if isinstance(mc, int) and isinstance(ma, int):
                    max_cll = max_cll or f"{mc},{ma}"
            elif "DOVI" in kind.upper() or "Dolby Vision" in kind:
                dovi = True
    return master_display, max_cll, dovi


def ffprobe_media(ffprobe: str, path: Path) -> Media:
    cmd = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    res = run_capture(cmd)
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe 读取失败: {(res.stderr or '').strip() or path}")
    data = json.loads(res.stdout or "{}")

    videos = [s for s in data.get("streams", [])
              if s.get("codec_type") == "video"
              and s.get("disposition", {}).get("attached_pic") != 1]
    audios = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not videos:
        raise RuntimeError(f"{path.name} 里找不到视频流（是不是只有音频/封面图？）")

    v = videos[0]
    m = Media(path=path)
    m.v_index = int(v.get("index", 0))
    m.width = int(v.get("width") or 0)
    m.height = int(v.get("height") or 0)
    m.fps = parse_fps(v.get("r_frame_rate") or v.get("avg_frame_rate") or "0/0")
    m.audio = [a.get("codec_name", "?") for a in audios]

    # ---- 色彩 / HDR ----
    m.pix_fmt = clean_tag(v.get("pix_fmt"))
    m.depth = pix_fmt_depth(m.pix_fmt)
    m.color_primaries = clean_tag(v.get("color_primaries"))
    m.color_transfer = clean_tag(v.get("color_transfer"))
    m.color_space = clean_tag(v.get("color_space"))
    m.color_range = clean_tag(v.get("color_range"))
    m.hdr = HDR_TRANSFERS.get(m.color_transfer, "")
    for side in v.get("side_data_list") or []:
        kind = str(side.get("side_data_type") or "")
        if "DOVI" in kind.upper() or "Dolby Vision" in kind:
            m.dovi = True
        elif kind.startswith("Mastering display"):
            m.master_display = master_display_param(side)
        elif kind.startswith("Content light level"):
            mc, ma = side.get("max_content"), side.get("max_average")
            if isinstance(mc, int) and isinstance(ma, int):
                m.max_cll = f"{mc},{ma}"
    if m.hdr:      # 静态 HDR 元数据只在帧的 side data 里，多跑一次 ffprobe 解一帧
        md, cll, dovi = ffprobe_hdr_metadata(ffprobe, path)
        m.master_display = m.master_display or md
        m.max_cll = m.max_cll or cll
        m.dovi = m.dovi or dovi

    for src in (data.get("format", {}).get("duration"), v.get("duration")):
        try:
            m.duration = float(src)  # type: ignore[arg-type]
            if m.duration > 0:
                break
        except (TypeError, ValueError):
            m.duration = 0.0
    return m


def gop_for_fps(fps: float) -> int:
    """关键帧间隔取约 2 秒，保证拖动条手感正常。"""
    if fps <= 0:
        return 120
    return max(1, int(round(fps * 2)))


# ==========================================================================
# 硬件探测
# ==========================================================================
def cpu_cores() -> int:
    try:
        return len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except AttributeError:
        return os.cpu_count() or 4


def detect_gpus() -> list[str]:
    """采集显卡名称：nvidia-smi -> wmic -> PowerShell。"""
    names: list[str] = []

    smi = shutil.which("nvidia-smi")
    if not smi:
        for p in (r"C:\Windows\System32\nvidia-smi.exe",
                  r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
            if Path(p).is_file():
                smi = p
                break
    if smi:
        r = run_capture([smi, "--query-gpu=name", "--format=csv,noheader"])
        if r.returncode == 0:
            names += [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]

    if os.name == "nt":
        r = run_capture(["wmic", "path", "win32_VideoController", "get", "name"])
        if r.returncode == 0:
            names += [ln.strip() for ln in (r.stdout or "").splitlines()
                      if ln.strip() and ln.strip().lower() != "name"]
        if not names:
            ps = "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"
            r = run_capture(["powershell", "-NoProfile", "-Command", ps])
            if r.returncode == 0:
                names += [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]

    seen, out = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def vendors_in(gpus: list[str]) -> list[str]:
    text = " | ".join(gpus).lower()
    found = []
    for vendor, keys in (("nvidia", ("nvidia", "geforce", "rtx", "gtx", "quadro", "tesla")),
                         ("amd", ("amd", "radeon", "ati ")),
                         ("intel", ("intel", "arc ", "iris", "uhd graphics", "hd graphics"))):
        if any(k in text for k in keys):
            found.append(vendor)
    return found


_ENC_CACHE: dict[str, bool] = {}


def encoder_available(ffmpeg: str, encoder: str) -> bool:
    """
    真的编 3 帧来验证编码器可用。
    只看 `ffmpeg -encoders` 不够：列表里有 nvenc，不代表当前显卡/驱动能跑。
    """
    if encoder in _ENC_CACHE:
        return _ENC_CACHE[encoder]
    if encoder not in list_encoders(ffmpeg):   # 编译里都没有，不用试
        _ENC_CACHE[encoder] = False
        return False
    cmd = [ffmpeg, "-hide_banner", "-v", "error",
           "-f", "lavfi", "-i", "color=c=black:s=320x240:r=30:d=0.1",
           "-pix_fmt", "yuv420p", "-c:v", encoder, "-f", "null", "-"]
    good = run_capture(cmd).returncode == 0
    _ENC_CACHE[encoder] = good
    return good


def pick_cpu_encoder(ffmpeg: str, codec: str) -> str | None:
    for enc in CPU_ENCODERS[codec]:
        if encoder_available(ffmpeg, enc):
            return enc
    return None


def vendor_usable(ffmpeg: str, vendor: str) -> bool:
    """该厂商至少有一个编码格式能真跑起来。"""
    return any(encoder_available(ffmpeg, HW_ENCODERS[vendor][c]) for c in ("h264", "h265", "av1"))


def usable_accels(ffmpeg: str, gpus: list[str]) -> list[str]:
    """返回实测可用的加速方案列表，CPU 永远在最后兜底。"""
    out = [v for v in vendors_in(gpus) if vendor_usable(ffmpeg, v)]
    out.append("cpu")
    return out


def acceler_encoders(ffmpeg: str, accel: str) -> list[str]:
    """该方案下实际可用的编码格式列表。"""
    if accel == "cpu":
        return [c for c in ("h264", "h265", "av1") if pick_cpu_encoder(ffmpeg, c)]
    return [c for c in ("h264", "h265", "av1") if encoder_available(ffmpeg, HW_ENCODERS[accel][c])]


# ==========================================================================
# 字幕处理
# ==========================================================================
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")


def read_text_any(path: Path) -> tuple[str, str]:
    """
    读字幕文本，自动识别编码，返回 (文本, 编码名)。

    麻烦之处：GBK 的字节能"恰好"组成合法 UTF-8 序列（例如 GBK 的"一"= D2 BB
    正好是合法 UTF-8），所以不能只靠"UTF-8 能不能解码"来判断。
    补充规则：UTF-8 解出来既不是纯 ASCII 也不含汉字，而 GBK 解出来含汉字 -> 按 GBK。
    """
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-bom"
    for bom, enc in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if raw.startswith(bom):
            try:
                return raw.decode(enc), enc
            except UnicodeDecodeError:
                break

    text_u = None
    try:
        text_u = raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    text_g = None
    try:
        text_g = raw.decode("gb18030")
    except UnicodeDecodeError:
        pass

    if text_u is not None:
        if text_u.isascii() or CJK_RE.search(text_u):
            return text_u, "utf-8"
        if text_g is not None and CJK_RE.search(text_g):
            return text_g, "gb18030"          # UTF-8 解出乱码而 GBK 解出汉字
        return text_u, "utf-8"

    if text_g is not None:
        return text_g, "gb18030"
    for enc in ("big5", "cp1252"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


# --- HDR 字幕配色预映射 ---------------------------------------------------
def _srgb_to_linear(code: float) -> float:
    """8bit sRGB 码值 -> 线性光（0..1）。"""
    v = max(0.0, min(255.0, float(code))) / 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _pq_signal(nits: float) -> float:
    """绝对亮度 -> PQ 信号（0..1，10000nit 为 1）。"""
    y = max(0.0, float(nits)) / 10000.0
    ym = y ** PQ_M1
    return ((PQ_C1 + PQ_C2 * ym) / (1.0 + PQ_C3 * ym)) ** PQ_M2


def _hlg_signal(nits: float) -> float:
    """显示亮度 -> HLG 信号（0..1，参考 1000nit 峰值）。"""
    e = (max(0.0, float(nits)) / HDR_HLG_PEAK_NITS) ** (1.0 / HDR_HLG_GAMMA)
    if e <= 1.0 / 12.0:
        return math.sqrt(3.0 * e)
    return HLG_A * math.log(12.0 * e - HLG_B) + HLG_C


_HDR_COLOR_CACHE: dict[tuple[int, int, int, str, bool], tuple[int, int, int]] = {}


def map_sdr_color_to_hdr(rgb: tuple[int, int, int], transfer: str,
                         peak_luma: bool = False) -> tuple[int, int, int]:
    """
    把一个 SDR 字幕颜色（BT.709 / sRGB）映射成 HDR 视频里该用的码值。

    线性光下 709 -> 2020 换色域，再按 203nit 图形白编码成 PQ 或 HLG 信号。
    白色在 PQ 下是 148、HLG 下是 191；黑色仍是 0，中间色按亮度比例变化。

    peak_luma=True 用于卡拉OK的"亮起"词：保留色相，把该颜色在线性光下整体
    缩放到 HDR_HIGHLIGHT_NITS（默认 1000nit）。这样高亮色不管是白、橙、青还是紫，
    都能顶满目标亮度，而不会被按原色亮度映射成偏暗的颜色。
    """
    key = (int(rgb[0]), int(rgb[1]), int(rgb[2]), transfer, bool(peak_luma))
    cached = _HDR_COLOR_CACHE.get(key)
    if cached is not None:
        return cached
    lin = [_srgb_to_linear(c) for c in key[:3]]
    channels = [max(0.0, row[0] * lin[0] + row[1] * lin[1] + row[2] * lin[2])
                for row in BT709_TO_BT2020]
    if peak_luma:
        # BT.2020 亮度系数；把颜色缩放到目标亮度（逐词高亮默认 1000nit）
        luma = 0.2627 * channels[0] + 0.6780 * channels[1] + 0.0593 * channels[2]
        if luma > 1e-6:
            scale = (HDR_HIGHLIGHT_NITS / HDR_SUB_WHITE_NITS) / luma
            channels = [c * scale for c in channels]
        else:
            channels = [0.0, 0.0, 0.0]
    out: list[int] = []
    for value in channels:
        nits = value * HDR_SUB_WHITE_NITS
        signal = _pq_signal(nits) if transfer != "arib-std-b67" else _hlg_signal(nits)
        out.append(max(0, min(255, int(round(signal * 255.0)))))
    result = (out[0], out[1], out[2])
    _HDR_COLOR_CACHE[key] = result
    return result


_ASS_COLOR_RE = re.compile(r"^&[Hh]([0-9A-Fa-f]{1,8})$")


def parse_ass_color(value: str) -> tuple[int, int, int, int, int] | None:
    """
    解析 ASS/SSA 颜色，返回 (r, g, b, a, 位宽)。
    ASS 是 &HAABBGGRR（6 位时省略 AA），老 SSA 也可能是十进制。
    """
    text = (value or "").strip()
    if not text:
        return None
    m = _ASS_COLOR_RE.match(text)
    if m:
        digits = m.group(1)
        number = int(digits, 16)
        width = 8 if len(digits) > 6 else 6
    elif text.isdigit():
        number = int(text, 10)
        width = 8
    else:
        return None
    a = (number >> 24) & 0xFF if width == 8 else 0
    b = (number >> 16) & 0xFF
    g = (number >> 8) & 0xFF
    r = number & 0xFF
    return r, g, b, a, width


def format_ass_color(rgb: tuple[int, int, int], alpha: int, width: int) -> str:
    r, g, b = rgb
    if width == 6:
        return f"&H{b:02X}{g:02X}{r:02X}"
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


_HDR_INLINE_COLOR_RE = re.compile(r"\\([1-4]?[cC])&[Hh]([0-9A-Fa-f]{1,8})(&?)")

# 逐词高亮（SubFabric 那类"每个词单独一条事件"的写法）：
#   {\c&HFEFFFE&\3c&HFF00FF&\3a&H40&\blur12.0}the{\c\3c\3a\blur}
# 起始块设置主色/描边色/透明度/模糊，单词后面用无参数的 \c 复位。
# 只对 Default 样式做"高亮词顶满 1000nit"，其他样式（翻译/特效）保持普通映射。
HDR_HIGHLIGHT_STYLE = "Default"
_HIGHLIGHT_START_RE = re.compile(
    r"\{[^}]*\\1?c&H[0-9A-Fa-f]{6}&[^}]*\}", re.IGNORECASE)
_HIGHLIGHT_RESET_RE = re.compile(r"\{\\c(?![&H])[^}]*\}", re.IGNORECASE)


def highlight_spans(line: str) -> list[tuple[int, int]]:
    """
    找出一行事件里"亮起的词"的字符区间 [start, end)。
    起点是带 \\c&H...& 的覆盖块，终点是后面第一个不带颜色的 \\c 复位块。
    没有复位块的不算（普通彩色文字），避免把整行都当成高亮。
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        start = _HIGHLIGHT_START_RE.search(line, pos)
        if start is None:
            break
        reset = _HIGHLIGHT_RESET_RE.search(line, start.end())
        if reset is None:
            break
        spans.append((start.start(), reset.start()))
        pos = reset.start()
    return spans


def remap_ass_colors(text: str, transfer: str,
                     karaoke_peak: bool = True) -> tuple[str, int]:
    """
    把 ASS/SSA 里所有 SDR 颜色改成 HDR 码值：样式表 4 个颜色 + 内联 \\c/\\1c..\\4c。
    返回 (新文本, 改动处数)。事件文本里的内联覆盖也一并处理，避免彩色字幕过曝。

    karaoke_peak=True 时，Default 样式里"亮起的词"（\\c&H...& ... {\\c...} 区间）
    的主色和描边色会额外顶满到 1000nit；普通文字仍按 203nit 图形白映射。
    """
    lines = text.splitlines()
    newline = "\r\n" if "\r\n" in text else "\n"
    changed = 0
    in_styles = False
    in_events = False
    fields: list[str] | None = None
    event_fields: list[str] | None = None
    default_fields = ["name", "fontname", "fontsize", "primarycolour", "secondarycolour",
                      "outlinecolour", "backcolour", "bold", "italic", "underline",
                      "strikeout", "scalex", "scaley", "spacing", "angle", "borderstyle",
                      "outline", "shadow", "alignment", "marginl", "marginr", "marginv",
                      "encoding"]
    default_event_fields = ["layer", "start", "end", "style", "name", "marginl",
                            "marginr", "marginv", "effect", "text"]

    for i, line in enumerate(lines):
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("[") and low.endswith("]"):
            in_styles = low in ("[v4+ styles]", "[v4 styles]", "[v4++ styles]")
            in_events = low == "[events]"
            fields = None
            event_fields = None
            continue

        if in_styles:
            if low.startswith("format:"):
                fields = [p.strip().lower() for p in stripped.split(":", 1)[1].split(",")]
                continue
            if low.startswith("style:"):
                body = stripped.split(":", 1)[1]
                use_fields = fields or default_fields
                parts = body.split(",", len(use_fields) - 1)
                if len(parts) == len(use_fields):
                    touched = False
                    for key in ("primarycolour", "secondarycolour", "outlinecolour",
                                "tertiarycolour", "backcolour"):
                        if key not in use_fields:
                            continue
                        idx = use_fields.index(key)
                        parsed = parse_ass_color(parts[idx])
                        if parsed is None:
                            continue
                        r, g, b, a, width = parsed
                        mapped = map_sdr_color_to_hdr((r, g, b), transfer)
                        if mapped != (r, g, b):
                            parts[idx] = format_ass_color(mapped, a, width)
                            touched = True
                    if touched:
                        lines[i] = line[:len(line) - len(line.lstrip())] + "Style:" + ",".join(parts)
                        changed += 1
                continue

        if in_events and low.startswith("format:"):
            event_fields = [p.strip().lower() for p in stripped.split(":", 1)[1].split(",")]
            continue

        if low.startswith(("dialogue:", "comment:")):
            spans: list[tuple[int, int]] = []
            if karaoke_peak:
                use_fields = event_fields or default_event_fields
                parts = stripped.split(":", 1)[1].split(",", len(use_fields) - 1)
                if len(parts) == len(use_fields):
                    style_idx = use_fields.index("style") if "style" in use_fields else 3
                    style_name = parts[style_idx].strip() if style_idx < len(parts) else ""
                    if style_name.lower() == HDR_HIGHLIGHT_STYLE.lower():
                        spans = highlight_spans(line)

            def repl(match: re.Match[str], spans: list[tuple[int, int]] = spans) -> str:
                prefix, digits, tail = match.group(1), match.group(2), match.group(3)
                width = 8 if len(digits) > 6 else 6
                number = int(digits, 16)
                a = (number >> 24) & 0xFF if width == 8 else 0
                b = (number >> 16) & 0xFF
                g = (number >> 8) & 0xFF
                r = number & 0xFF
                tag = prefix.lower()
                in_highlight = (tag in ("c", "1c", "3c")
                                and any(start <= match.start() < end for start, end in spans))
                nr, ng, nb = map_sdr_color_to_hdr((r, g, b), transfer,
                                                 peak_luma=in_highlight)
                digits_out = (f"{a:02X}{nb:02X}{ng:02X}{nr:02X}" if width == 8
                              else f"{nb:02X}{ng:02X}{nr:02X}")
                return f"\\{prefix}&H{digits_out}{tail}"

            new_line, count = _HDR_INLINE_COLOR_RE.subn(repl, line)
            if count:
                lines[i] = new_line
                changed += count

    return newline.join(lines), changed


def hdr_sub_force_style(transfer: str) -> str:
    """
    SRT/VTT 没有样式表，libass 会用默认白色。给 subtitles 滤镜传 force_style，
    把默认白/描边改写成 HDR 码值（只影响默认样式，不碰字号/位置）。
    """
    white = map_sdr_color_to_hdr((255, 255, 255), transfer)
    value = f"&H00{white[2]:02X}{white[1]:02X}{white[0]:02X}"
    return (f"PrimaryColour={value},SecondaryColour={value},"
            "OutlineColour=&H00000000,BackColour=&H00000000")


# --- 首条字幕保护 ---------------------------------------------------------
# 现象：ffmpeg 的 subtitles 滤镜读 ASS 时，如果文件里事件数较多（实测 >50 条），
#       会把【文件中的第一条事件】丢掉不渲染 —— 视频开头的第一条字幕就没了。
# 实测（把字幕烧到黑底视频上逐帧统计）：
#       事件 ≤50 条：字幕从 0.04s 开始  ✓
#       事件 ≥100 条：字幕从 0.52s 才开始 ✗（第一条 0.03-0.51s 的事件被吞）
# 规避：在 [Events] 段的 Format 行后面插一条空文本、时长 10ms 的事件当"垫背"，
#       被吞的是它；真实的第一条字幕照常渲染。事件少时插入它也毫无副作用。
SACRIFICIAL_START = "0:00:00.00"
SACRIFICIAL_END = "0:00:00.01"
DEFAULT_FIELDS = ["Layer", "Start", "End", "Style", "Name", "MarginL", "MarginR",
                  "MarginV", "Effect", "Text"]


def inject_sacrificial_event(text: str) -> tuple[str, bool]:
    """在 ASS/SSA 的 [Events] 段首插一条空事件。返回 (新文本, 是否插入)。"""
    lines = text.splitlines()
    newline = "\r\n" if "\r\n" in text else "\n"

    ev_start = next((i for i, ln in enumerate(lines)
                     if ln.strip().lower().startswith("[events]")), None)
    if ev_start is None:
        return text, False

    fields, insert_at = DEFAULT_FIELDS, ev_start + 1
    for i in range(ev_start + 1, min(ev_start + 6, len(lines))):
        s = lines[i].strip()
        if s.lower().startswith("format:"):
            parts = [p.strip() for p in s.split(":", 1)[1].split(",")]
            if len(parts) >= 5:
                fields = parts
                insert_at = i + 1
            break

    low = [f.lower() for f in fields]
    if not {"start", "end", "text"} <= set(low):
        return text, False

    ev = {f: "" for f in fields}
    ev[fields[low.index("start")]] = SACRIFICIAL_START
    ev[fields[low.index("end")]] = SACRIFICIAL_END
    if "layer" in low:
        ev[fields[low.index("layer")]] = "0"
    if "style" in low:  # 借用第一条真实事件的样式名，兼容性最好
        for ln in lines[insert_at:]:
            if ln.strip().lower().startswith(("dialogue:", "comment:")):
                vals = ln.split(":", 1)[1].split(",", len(fields) - 1)
                if len(vals) == len(fields):
                    ev[fields[low.index("style")]] = vals[low.index("style")].strip()
                break
    ev[fields[low.index("text")]] = ""  # 空文本 -> 画面上不会出现任何东西

    lines.insert(insert_at, "Dialogue: " + ",".join(ev[f] for f in fields))
    return newline.join(lines), True


def inject_sacrificial_cue(text: str, kind: str) -> tuple[str, bool]:
    """SRT / VTT 也插一条空 cue 当垫背。"""
    nl = "\r\n" if "\r\n" in text else "\n"
    if kind == "srt":
        if not re.search(r"\d{1,2}:\d{2}:\d{2},\d{3}", text):
            return text, False
        cue = "0" + nl + "00:00:00,000 --> 00:00:00,010" + nl + " " + nl + nl
        return cue + text, True
    # vtt：插在 WEBVTT 头部之后
    if not text.lstrip("\ufeff").startswith("WEBVTT"):
        return text, False
    if "-->" not in text:
        return text, False
    first_cue = text.find("\n\n")
    if first_cue < 0:
        return text, False
    cue = nl + "00:00:00.000 --> 00:00:00.010" + nl + " " + nl
    pos = first_cue + 2
    return text[:pos] + cue + text[pos:], True


def prepare_subtitle(src: Path, workdir: Path,
                     hdr_transfer: str = "",
                     karaoke_peak: bool = True) -> tuple[Path, list[str]]:
    """
    把字幕复制到工作目录，并做三件事：
      1. 统一转成 UTF-8（GBK 老字幕也能被 libass 正确渲染）
      2. 注入"牺牲事件"，规避第一条字幕被吞的 bug
      3. HDR 片源：把 ASS/SSA 里的 SDR 颜色预映射成 PQ/HLG 码值
         （SRT/VTT 没有样式表，由 hdr_sub_force_style() 交给 subtitles 滤镜）
         逐词高亮的 Default 行还会把高亮词顶满到 1000nit。
    """
    notes: list[str] = []
    text, enc = read_text_any(src)
    if enc not in ("utf-8", "utf-8-sig", "utf-8-bom"):
        notes.append(f"字幕原编码 {enc}，已统一转换为 UTF-8")

    ext = src.suffix.lower()
    tmp = workdir / ("hardsub_sub" + ext)
    if ext in (".ass", ".ssa"):
        text, done = inject_sacrificial_event(text)
    elif ext in (".srt", ".vtt"):
        text, done = inject_sacrificial_cue(text, ext[1:])
    else:
        done = False
    if done:
        notes.append("已插入首条字幕保护（规避 ffmpeg 吞掉第一条字幕）")

    if hdr_transfer and ext in (".ass", ".ssa"):
        text, colors = remap_ass_colors(text, hdr_transfer, karaoke_peak=karaoke_peak)
        if colors:
            notes.append(f"HDR 字幕配色已预映射（{HDR_TRANSFERS.get(hdr_transfer, hdr_transfer)}，"
                         f"SDR 白 ≈ {HDR_SUB_WHITE_NITS:.0f}nit）")

    tmp.write_text(text, encoding="utf-8", newline="")
    return tmp, notes


def escape_filter_path(path: str | Path) -> str:
    """
    把路径安全地塞进 -vf 滤镜参数。
    必须处理反斜杠和盘符冒号，否则 ffmpeg 会把 `D:` 的冒号当成选项分隔符：
      Unable to parse "original_size" option value "D" as image size
    """
    p = str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return "'" + p + "'"


def find_fonts_dir(*bases: Path) -> Path | None:
    for base in bases:
        for name in ("fonts", "Fonts", "字体", "font"):
            d = base / name
            if d.is_dir():
                return d
    return None


# ==========================================================================
# 编码方案
# ==========================================================================
@dataclass
class Plan:
    accel: str
    codec: str
    encoder: str
    input_opts: list[str]
    enc_params: list[str]
    quality: int
    pix_fmt: str = "yuv420p"       # 滤镜链尾部 / 编码器像素格式
    hdr: bool = False
    color_opts: list[str] = field(default_factory=list)
    hw_pix_fmt: str = ""           # 硬件帧下载到内存后的格式（10bit=p010le）
    filters: str = ""


def accel_input_opts(accel: str) -> list[str]:
    return {
        "cpu": [],
        "nvidia": ["-init_hw_device", "cuda=cuda:0", "-hwaccel", "cuda",
                   "-hwaccel_output_format", "cuda"],
        "amd": ["-init_hw_device", "d3d11va=dx", "-hwaccel", "d3d11va",
                "-hwaccel_output_format", "d3d11"],
        "intel": ["-init_hw_device", "qsv=hw", "-filter_hw_device", "hw",
                  "-hwaccel", "qsv", "-hwaccel_output_format", "qsv"],
    }[accel]


def hw_upload_filter(accel: str) -> str:
    return {"nvidia": "hwupload_cuda",
            "amd": "hwupload=derive_device=d3d11va",
            "intel": "hwupload=derive_device=qsv"}[accel]


def build_filters(sub_filter: str, accel: str, pix_fmt: str = "yuv420p",
                  tonemap_filters: str = "", hw_pix_fmt: str = "") -> str:
    """
    组装滤镜链。

    HDR 的坑：10bit 源解到显存后是 p010 帧，`hwdownload` 只能输出 p010le；
    旧代码写死 `format=nv12`，会直接报 "Invalid output format nv12 for hwframe
    download"，硬件链路整条不可用。这里按位深选格式（8bit=nv12，10bit=p010le）。

    `hwdownload` 后面紧跟一个 format 把下载格式钉死也很重要：后面接 zscale 时，
    滤镜图会自己协商格式，曾经协商出 gray 直接把硬件链路搞挂。

    tonemap_filters 只在 --tonemap 时出现，且必须排在 subtitles 前面：
    先把 HDR 压成 SDR，再烧字幕，字幕才会是正常的 SDR 白色，而不是被一起压暗。
    """
    parts: list[str] = []
    if accel != "cpu":
        parts.append("hwdownload")
        if hw_pix_fmt:
            parts.append(f"format={hw_pix_fmt}")
    if tonemap_filters:
        parts.append(tonemap_filters)
    parts.append(sub_filter)
    parts.append(f"format={pix_fmt}")
    if accel != "cpu":
        parts.append(hw_upload_filter(accel))
    return ",".join(parts)


def hdr_encoder_profile(accel: str, encoder: str) -> list[str]:
    """10bit HDR 要让硬件编码器显式开 Main10 / High10，否则它会按 8bit 编码。"""
    profile = {("nvidia", "hevc_nvenc"): "main10",
               ("nvidia", "h264_nvenc"): "high10",
               ("amd", "hevc_amf"): "main10",
               ("intel", "hevc_qsv"): "main10"}.get((accel, encoder))
    return ["-profile:v", profile] if profile else []


def color_opts_for(media: Media | None, hdr: bool, tonemapped: bool) -> list[str]:
    """HDR 必须显式写回色彩标签，否则播放器会把 PQ 数据当 BT.709 显示（画面发灰）。"""
    if hdr and media is not None:
        return ["-color_primaries", "bt2020",
                "-color_trc", media.color_transfer or "smpte2084",
                "-colorspace", "bt2020nc", "-color_range", "tv"]
    if tonemapped:
        return ["-color_primaries", "bt709", "-color_trc", "bt709",
                "-colorspace", "bt709", "-color_range", "tv"]
    return []


TONEMAP_ZSCALE = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                  "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
TONEMAP_LIBPLACEBO = ("libplacebo=tonemapping=bt.2390:colorspace=bt709:"
                      "color_primaries=bt709:color_trc=bt709:format=yuv420p")


def pick_tonemap(ffmpeg: str) -> tuple[str, str]:
    """
    HDR → SDR 的滤镜链。
    优先 zscale + tonemap（纯 CPU、不依赖显卡驱动），没有 libzimg 时退到 libplacebo。
    """
    filters = list_filters(ffmpeg)
    if {"zscale", "tonemap"} <= filters:
        return TONEMAP_ZSCALE, "zscale + tonemap(hable)"
    if "libplacebo" in filters:
        return TONEMAP_LIBPLACEBO, "libplacebo(bt.2390)"
    raise RuntimeError("这个 ffmpeg 没有 zscale/tonemap 或 libplacebo 滤镜，无法 HDR→SDR；"
                       "请换用带 libzimg 的完整版 ffmpeg，或去掉 --tonemap 保留 HDR")


def hdr_x265_params(media: Media) -> list[str]:
    """x265 需要显式打开 HDR 支持，否则不写 HDR10 的 SEI。"""
    params = ["repeat-headers=1",
              f"colorprim={media.color_primaries or 'bt2020'}",
              f"transfer={media.color_transfer or 'smpte2084'}",
              f"colormatrix={media.color_space or 'bt2020nc'}"]
    if media.color_transfer == "smpte2084":
        # hdr-opt 是 HDR10（PQ）专用的；HLG 上开它 x265 会警告并自行关掉
        params.insert(0, "hdr-opt=1")
    if media.master_display:
        params.append(f"master-display={media.master_display}")
    if media.max_cll:
        params.append(f"max-cll={media.max_cll}")
    return params


def build_plan(ffmpeg: str, accel: str, codec: str, quality: int,
               media: Media | None = None, tonemapped: bool = False) -> Plan:
    hdr = bool(media and media.hdr and not tonemapped)
    if accel == "cpu":
        pix_fmt = "yuv420p10le" if hdr else "yuv420p"
    else:
        # 压缩前的像素格式：8bit 用 nv12，10bit 必须用 p010le
        pix_fmt = "p010le" if hdr else "nv12"
    # 硬件帧下载到内存后的格式由【源】的位深决定（10bit 源就是 p010le）
    hw_pix_fmt = ""
    if accel != "cpu":
        hw_pix_fmt = "p010le" if (media and media.depth > 8) else "nv12"
    color_opts = color_opts_for(media, hdr, tonemapped)

    if accel == "cpu":
        encoder = pick_cpu_encoder(ffmpeg, codec)
        if not encoder:
            raise RuntimeError(f"这个 ffmpeg 没有可用的 {codec} 软编码器")
        params = ["-crf", str(quality)]
        # libsvtav1 的 preset 是数字(0-13)，写 "medium" 会直接报错
        params += ["-preset", "8" if encoder == "libsvtav1" else "medium"]
        if hdr and encoder == "libx265" and media is not None:
            params += ["-x265-params", ":".join(hdr_x265_params(media))]
        return Plan("cpu", codec, encoder, [], params, quality,
                    pix_fmt=pix_fmt, hdr=hdr, color_opts=color_opts)

    encoder = HW_ENCODERS[accel][codec]
    if not encoder_available(ffmpeg, encoder):
        raise RuntimeError(f"{encoder} 不可用")
    if accel == "nvidia":
        params = ["-rc", "vbr", "-cq", str(quality), "-b:v", "0", "-preset", "p6"]
    elif accel == "amd":
        params = ["-rc", "cqp", "-qp_i", str(quality), "-qp_p", str(quality),
                  "-qp_b", str(quality), "-quality", "quality"]
    else:
        params = ["-global_quality", str(quality), "-look_ahead", "1"]
    if hdr:
        params += hdr_encoder_profile(accel, encoder)
    return Plan(accel, codec, encoder, accel_input_opts(accel), params, quality,
                pix_fmt=pix_fmt, hdr=hdr, color_opts=color_opts, hw_pix_fmt=hw_pix_fmt)


def default_quality_for(codec: str, accel: str) -> int:
    if codec == "av1":
        return 35 if accel == "cpu" else DEFAULT_QUALITY["av1"]
    return DEFAULT_QUALITY[codec]


def clamp_quality(codec: str, q: int) -> int:
    lo, hi = QUALITY_RANGE[codec]
    if q < lo:
        warn(f"画质参数 {q} 小于下限 {lo}，已改为 {lo}")
        return lo
    if q > hi:
        warn(f"画质参数 {q} 大于上限 {hi}，已改为 {hi}")
        return hi
    return q


def pick_output_ext(video: Path, codec: str) -> str:
    ext = video.suffix.lower()
    if ext == ".webm":
        return ".mkv"
    if ext in (".mp4", ".mov", ".m4v") and codec in ("h264", "h265"):
        return ".mp4"
    return ".mkv"


def sub_filter_opts(sub_tmp: Path, fonts_dir: Path | None,
                    force_style: str = "") -> str:
    s = "subtitles=" + escape_filter_path(sub_tmp)
    if force_style:
        s += ":force_style='" + force_style + "'"
    if fonts_dir:
        s += ":fontsdir=" + escape_filter_path(fonts_dir)
    return s


def build_ffmpeg_cmd(ffmpeg: str, plan: Plan, media: Media, sub_tmp: Path, out_path: Path,
                     fonts_dir: Path | None, threads: int, loglevel: str,
                     tonemap_filters: str = "", hdr_sub_style: str = "") -> list[str]:
    plan.filters = build_filters(sub_filter_opts(sub_tmp, fonts_dir, hdr_sub_style),
                                 plan.accel, plan.pix_fmt, tonemap_filters, plan.hw_pix_fmt)

    cmd = [ffmpeg, "-hide_banner", "-loglevel", loglevel, "-y", "-nostdin"]
    cmd += plan.input_opts
    cmd += ["-i", str(media.path), "-map", f"0:{media.v_index}"]
    if media.audio:
        cmd += ["-map", "0:a?"]
    cmd += ["-map_metadata", "0", "-vf", plan.filters, "-c:v", plan.encoder]
    cmd += plan.enc_params
    cmd += plan.color_opts                 # HDR: 显式写回 bt2020 / PQ(HLG) 标签
    cmd += ["-g", str(gop_for_fps(media.fps))]
    if plan.accel == "cpu":
        cmd += ["-pix_fmt", plan.pix_fmt]  # 硬件链路不能加，否则会把 hw 帧拉回内存
    if plan.codec == "h265" and out_path.suffix.lower() in (".mp4", ".mov"):
        cmd += ["-tag:v", "hvc1"]           # 让 QuickTime / 手机相册能认
    if media.audio:
        if out_path.suffix.lower() in (".mp4", ".mov") and any(a not in MP4_OK_AUDIO for a in media.audio):
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-c:a", "copy"]
    if out_path.suffix.lower() in (".mp4", ".mov"):
        cmd += ["-movflags", "+faststart"]
    if threads > 0:
        cmd += ["-threads", str(threads)]
    cmd += ["-progress", "pipe:1", "-nostats", str(out_path)]
    return cmd


_FFMPEG_PREFIX_RE = re.compile(r"^\s*\[[^\]]*@[^\]]*\]\s*")
_MEANINGFUL_ERR_RE = re.compile(
    r"(?i)(invalid|error|failed|not supported|no capable|unable|cannot|doesn't support)")


def clean_ffmpeg_line(line: str) -> str:
    return _FFMPEG_PREFIX_RE.sub("", line.strip())


def pick_error_line(stderr: str) -> str:
    """
    从 ffmpeg 的 stderr 里挑一条最像"根因"的。
    旧实现直接取最后一行，结果经常取到 "Nothing was written into output file"
    这种善后信息，真正的原因（比如 Invalid output format nv12）反而被吞掉。
    """
    lines = [clean_ffmpeg_line(ln) for ln in (stderr or "").splitlines() if ln.strip()]
    for line in lines:
        if _MEANINGFUL_ERR_RE.search(line) and "Nothing was written" not in line:
            return line
    return lines[-1] if lines else "未知错误"


def validate_chain(ffmpeg: str, plan: Plan, media: Media, sub_tmp: Path,
                   fonts_dir: Path | None, tonemap_filters: str = "",
                   hdr_sub_style: str = "") -> tuple[bool, str]:
    """
    正式压制前用真实视频试跑 0.5 秒整条链路（硬解 + 滤镜 + 编码）。
    硬件方案常常"能列出编码器但跑不动"，提前 0.5 秒验证可以避免白等一小时。
    """
    filters = build_filters(sub_filter_opts(sub_tmp, fonts_dir, hdr_sub_style), plan.accel,
                            plan.pix_fmt, tonemap_filters, plan.hw_pix_fmt)
    cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y"]
    cmd += plan.input_opts
    cmd += ["-i", str(media.path), "-t", "0.5", "-an", "-vf", filters,
            "-c:v", plan.encoder] + plan.enc_params + plan.color_opts + ["-f", "null", "-"]
    res = run_capture(cmd)
    if res.returncode == 0:
        return True, ""
    return False, pick_error_line(res.stderr or "")


# ==========================================================================
# 执行与校验
# ==========================================================================
def human_time(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class ProgressState:
    """ffmpeg -progress 的最新状态：读取线程写，主线程读。"""

    __slots__ = ("lock", "out_time", "frame", "speed", "ended")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.out_time = 0.0
        self.frame = 0
        self.speed = 0.0
        self.ended = False


def parse_progress_time(key: str, value: str) -> float | None:
    """
    把 -progress 的时间字段统一成秒。

    注意两点：
      * ffmpeg 的 out_time_ms 历史上写的其实是微秒（和 out_time_us 同值），不是毫秒；
      * 老版本 ffmpeg 只有 out_time=HH:MM:SS.ffffff，没有 out_time_us。
    所以三种字段都要认，否则在"某些 ffmpeg 版本"下进度条会一直不动。
    """
    value = value.strip()
    if key in ("out_time_us", "out_time_ms"):
        if not value.isdigit():
            return None            # 还没编出第一帧时是 N/A
        return int(value) / 1_000_000
    m = re.match(r"^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$", value)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _progress_reader(proc: subprocess.Popen, state: ProgressState) -> None:
    """持续读 stdout（-progress 输出），主线程只负责画，永远不让管道写满。"""
    try:
        stream = proc.stdout
        if stream is None:
            return
        for raw in stream:
            line = raw.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            with state.lock:
                if key in ("out_time_us", "out_time_ms", "out_time"):
                    seconds = parse_progress_time(key, value)
                    if seconds is not None and seconds > state.out_time:
                        state.out_time = seconds
                elif key == "frame" and value.isdigit():
                    state.frame = int(value)
                elif key == "speed" and value.endswith("x"):
                    try:
                        state.speed = float(value[:-1])
                    except ValueError:
                        pass
                elif key == "progress" and value == "end":
                    state.ended = True
    except (ValueError, OSError):
        pass


def _stderr_reader(proc: subprocess.Popen, sink: deque, count: list) -> None:
    """
    把 stderr 读干净并留档。

    这是进度条"卡死"的关键：ffmpeg 的警告写 stderr，如果父进程把 stderr 给了管道
    却没人读，Windows 的管道缓冲区（64KB）写满后 ffmpeg 就阻塞在写日志上——
    进程还在、视频却没继续编，进度自然一动不动。这里必须有专门的线程一直读。
    """
    try:
        stream = proc.stderr
        if stream is None:
            return
        for raw in stream:
            line = raw.rstrip("\r\n")
            if line.strip():
                sink.append(line)
                count[0] += 1
    except (ValueError, OSError):
        pass


def _progress_text(done: float, duration: float, speed: float,
                   stalled: bool, ended: bool) -> str:
    width = 32
    if duration > 0:
        pct = max(0.0, min(1.0, done / duration))
        filled = int(width * pct)
        bar = "█" * filled + "░" * (width - filled)
        if ended or pct >= 0.999:
            tail = "收尾中…"
            eta = 0.0
        else:
            eta = (duration - done) / speed if speed > 0 else -1
            tail = f"剩余 {human_time(eta)}"
        text = f"  {bar} {pct * 100:5.1f}%  {tail}  ({human_time(done)}/{human_time(duration)})"
    else:
        text = f"  已处理 {human_time(done)}  速度 {speed:.2f}x"
    if stalled:
        text += "  [等待 ffmpeg…]"
    return text


def _draw_progress(text: str) -> None:
    sys.stdout.write("\r" + text + " " * 100)
    sys.stdout.flush()


def _report_ffmpeg_output(lines: deque, count: int, failed: bool, verbose: bool) -> None:
    """把 ffmpeg 的警告/错误汇总给用户看，而不是让它们糊在进度条上。"""
    if not lines:
        return
    cleaned = [clean_ffmpeg_line(ln) for ln in lines]
    if failed:
        log("")
        warn(f"ffmpeg 日志（共 {count} 行），最后 12 行：")
        for line in cleaned[-12:]:
            log(f"  {line}")
        return
    if verbose:
        log("")
        info(f"ffmpeg 日志（共 {len(cleaned)} 行）：")
        for line in cleaned:
            log(f"  [ffmpeg] {line}")
        return
    # SVT-AV1 之类的外部编码库会把 [info] 直接写到 stderr，别当警告吓人
    cleaned = [ln for ln in cleaned if "[info]" not in ln.lower()]
    if not cleaned:
        return
    seen: set[str] = set()
    unique: list[str] = []
    for line in cleaned:
        key = re.sub(r"\d+", "#", line)      # 数字归一化后再去重，DTS 警告只留一条
        if key not in seen:
            seen.add(key)
            unique.append(line)
    warn(f"ffmpeg 报告了 {len(cleaned)} 条警告（去重后 {len(unique)} 条）：")
    for line in unique[:5]:
        log(f"  {line}")
    if len(unique) > 5:
        log(f"  …还有 {len(unique) - 5} 条")


def run_encode(cmd: list[str], duration: float, verbose: bool = False) -> int:
    """
    跑 ffmpeg，解析 -progress 画进度条。

    相比 1.0：
      * stdout / stderr 各一个读取线程 —— ffmpeg 刷多少警告都不会把管道写满卡死；
      * out_time_us 缺失时退回 out_time_ms / out_time —— 老版本 ffmpeg 也能出进度；
      * 长时间没有新进度会标 [等待 ffmpeg…]，不会像死机；
      * 结束后汇总警告，失败时打印真正的错误行。
    """
    tty = sys.stdout.isatty()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    state = ProgressState()
    err_lines: deque = deque(maxlen=500)
    err_count = [0]
    threads = [threading.Thread(target=_progress_reader, args=(proc, state), daemon=True),
               threading.Thread(target=_stderr_reader, args=(proc, err_lines, err_count), daemon=True)]
    for thread in threads:
        thread.start()

    start = time.time()
    last_done = -1.0
    last_change = start
    last_draw = 0.0
    last_report = 0.0
    try:
        while proc.poll() is None:
            now = time.time()
            with state.lock:
                done, speed, ended = state.out_time, state.speed, state.ended
            if speed <= 0 and done > 0:
                speed = done / max(0.001, now - start)   # 老版 ffmpeg 没有 speed 字段
            if done > last_done + 1e-4:
                last_done, last_change = done, now
            stalled = not ended and (now - last_change) > 8.0
            if tty:
                if now - last_draw >= 0.25:
                    last_draw = now
                    _draw_progress(_progress_text(done, duration, speed, stalled, ended))
            elif now - last_report >= 5.0:
                last_report = now
                if duration > 0:
                    pct = max(0.0, min(1.0, done / duration))
                    if ended or pct >= 0.999:
                        tail = "收尾中…"
                    else:
                        eta = (duration - done) / speed if speed > 0 else -1
                        tail = f"剩余 {human_time(eta)}"
                    log(f"  进度 {pct * 100:5.1f}%  {tail}" + ("  [等待 ffmpeg…]" if stalled else ""))
                else:
                    log(f"  已处理 {human_time(done)}" + ("  [等待 ffmpeg…]" if stalled else ""))
            time.sleep(0.15)
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise
    finally:
        if tty:
            sys.stdout.write("\r" + " " * 100 + "\r")
            sys.stdout.flush()
        code = proc.wait()
        for thread in threads:
            thread.join(timeout=1.5)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    _report_ffmpeg_output(err_lines, err_count[0], failed=(code != 0), verbose=verbose)
    return code


def verify_output(ffprobe: str, out_path: Path, src: Media,
                  hdr_expected: bool = False) -> list[str]:
    if not out_path.is_file() or out_path.stat().st_size == 0:
        return [f"输出文件不存在或为空: {out_path}"]
    try:
        got = ffprobe_media(ffprobe, out_path)
    except Exception as e:  # noqa: BLE001
        return [f"输出文件无法解析: {e}"]
    issues: list[str] = []
    if src.duration and got.duration and abs(got.duration - src.duration) > 1.0:
        issues.append(f"时长不一致: {src.duration:.2f}s -> {got.duration:.2f}s")
    if (src.width, src.height) != (got.width, got.height):
        issues.append(f"分辨率变化: {src.width}x{src.height} -> {got.width}x{got.height}")
    if len(src.audio) != len(got.audio):
        issues.append(f"音轨数变化: {len(src.audio)} -> {len(got.audio)}")
    if hdr_expected:
        if got.depth < 10:
            issues.append(f"HDR 输出成了 {got.depth}bit（{got.pix_fmt or '?'}），应为 10bit")
        if got.color_transfer != src.color_transfer:
            issues.append(f"HDR 传递特性丢失: {src.color_transfer or '?'} -> {got.color_transfer or '未标记'}")
        if got.color_primaries != src.color_primaries:
            issues.append(f"HDR 色域标记丢失: {src.color_primaries or '?'} -> {got.color_primaries or '未标记'}")
    return issues


# ==========================================================================
# 扫描与交互
# ==========================================================================
def scan_dir(directory: Path) -> tuple[list[Path], list[Path]]:
    videos, subs = [], []
    for p in sorted(directory.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        e = p.suffix.lower()
        if e in VIDEO_EXTS:
            videos.append(p)
        elif e in SUB_EXTS:
            subs.append(p)
    return videos, subs


def ask_choice(prompt: str, count: int, default: int | None = None) -> int:
    """返回 0 起的下标。输入非法会重问。"""
    while True:
        hint = f"（1-{count}" + (f"，回车={default}" if default else "") + "）"
        try:
            raw = input(f"{prompt}{hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            log("")
            raise SystemExit(130)
        if not raw:
            if default:
                return default - 1
            log("  不能为空，请重新输入。")
            continue
        if not raw.isdigit():
            log("  请输入纯数字。")
            continue
        n = int(raw)
        if 1 <= n <= count:
            return n - 1
        log("  编号超出范围。")


def ask_text(prompt: str, default: str = "") -> str:
    try:
        raw = input(f"{prompt}{f'（回车={default}）' if default else ''}: ").strip()
    except (EOFError, KeyboardInterrupt):
        log("")
        raise SystemExit(130)
    return raw or default


def ask_yes(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        raw = input(f"{prompt} [{hint}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        log("")
        raise SystemExit(130)
    return default if not raw else raw in ("y", "yes", "是", "好", "1")


def print_encoder_report(ffmpeg: str) -> None:
    log("编码器实测（✓ = 真的编过 3 帧验证通过）：")
    for codec in ("h264", "h265", "av1"):
        parts = []
        for vendor in ("nvidia", "amd", "intel"):
            enc = HW_ENCODERS[vendor][codec]
            parts.append(f"{vendor} {C.G}✓{C.R}" if encoder_available(ffmpeg, enc)
                         else f"{vendor} {C.E}✗{C.R}")
        enc = pick_cpu_encoder(ffmpeg, codec)
        parts.append(f"cpu {C.G}✓ ({enc}){C.R}" if enc else f"cpu {C.E}✗{C.R}")
        log(f"  {CODEC_LABEL[codec]:12s} " + "   ".join(parts))


def probe_report(ffmpeg: str | None, ffprobe: str | None) -> int:
    log(f"{C.B}hardsub {VERSION} 环境自检{C.R}")
    log("-" * 64)
    if not ffmpeg or not ffprobe:
        err("未找到 ffmpeg / ffprobe。请把它们放到本程序同目录，或加入系统 PATH。")
        return 2
    log(f"ffmpeg   : {ffmpeg}")
    log(f"           版本 {ffmpeg_version(ffmpeg)}")
    log(f"ffprobe  : {ffprobe}")
    log(f"CPU      : {cpu_cores()} 核心")
    gpus = detect_gpus()
    log(f"显卡     : {' / '.join(gpus) if gpus else '未检测到'}")
    log(f"操作系统 : {sys.platform}")
    log("-" * 64)
    print_encoder_report(ffmpeg)
    log("-" * 64)
    accels = usable_accels(ffmpeg, gpus)
    log("可用加速方案: " + ", ".join(VENDOR_LABEL[a] for a in accels))
    return 0


# ==========================================================================
# 主流程
# ==========================================================================
def main(argv: list[str] | None = None) -> int:
    ap = SafeArgumentParser(
        prog="hardsub",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="智能硬编码字幕：把 ASS/SSA/SRT/VTT 字幕永久烧进视频画面。",
        epilog="不带参数运行 = 交互式模式；先跑一次 --probe 可以看到本机支持哪些硬件编码。")
    ap.add_argument("video", nargs="?", help="输入视频（可省略，交互选择）")
    ap.add_argument("-v", "--video", dest="video_opt", help="输入视频")
    ap.add_argument("-s", "--subtitle", help="字幕文件（.ass/.ssa/.srt/.vtt）")
    ap.add_argument("-o", "--output", help="输出文件（默认 <名字>_hardsub_<编码>）")
    ap.add_argument("-d", "--dir", dest="workdir", help="工作目录（默认当前目录）")
    ap.add_argument("-a", "--accel", default="auto",
                    choices=["auto", "cpu", "nvidia", "amd", "intel"], help="加速方案（默认 auto）")
    ap.add_argument("-c", "--codec", default=None, choices=["h264", "h265", "av1"], help="编码格式")
    ap.add_argument("-q", "--quality", type=int, default=None,
                    help="画质参数 crf/cq/qp，越小越清晰（h264/h265 默认 23，av1 默认 30）")
    ap.add_argument("--preset", help="CPU 软编码 preset（medium / slow / veryslow ...）")
    ap.add_argument("--fontsdir", help="额外字体目录（字幕用到的字体放这里）")
    ap.add_argument("-t", "--threads", type=int, default=0, help="限制编码线程数（0=自动）")
    ap.add_argument("-y", "--yes", action="store_true", help="全部使用默认值，不再询问")
    ap.add_argument("--overwrite", action="store_true", help="输出文件已存在时直接覆盖")
    ap.add_argument("--keep-temp", action="store_true", help="保留临时字幕文件")
    ap.add_argument("--dry-run", action="store_true", help="只打印 ffmpeg 命令，不执行")
    ap.add_argument("--tonemap", action="store_true",
                    help="HDR→SDR 色调映射（输出 BT.709 8bit，给普通屏幕看）")
    ap.add_argument("--verbose", action="store_true", help="结束时打印完整的 ffmpeg 日志")
    ap.add_argument("--list", action="store_true", help="列出目录里的视频和字幕")
    ap.add_argument("--probe", action="store_true", help="环境自检")
    ap.add_argument("--no-pause", action="store_true", help="结束时不等待回车")
    ap.add_argument("--version", action="version", version=f"hardsub {VERSION}")
    args = ap.parse_args(argv)

    ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")

    if args.probe:
        return probe_report(ffmpeg, ffprobe)

    log(f"{C.B}=== 智能硬编码字幕 v{VERSION} ==={C.R}")
    if not ffmpeg or not ffprobe:
        err("未找到 ffmpeg / ffprobe。请把它们放到本程序同目录，或加入系统 PATH。")
        return 2
    info(f"ffmpeg {ffmpeg_version(ffmpeg)}")

    wd = Path(args.workdir or ".").resolve()
    if not wd.is_dir():
        err(f"目录不存在: {wd}")
        return 2

    videos, subs = scan_dir(wd)
    if args.list:
        log(f"目录: {wd}")
        log(f"视频（{len(videos)}）: " + (", ".join(v.name for v in videos) if videos else "无"))
        log(f"字幕（{len(subs)}）: " + (", ".join(s.name for s in subs) if subs else "无"))
        return 0

    interactive = not args.yes and sys.stdin.isatty()

    # ---------- 输入视频 ----------
    video_arg = args.video_opt or args.video
    if video_arg:
        video = Path(video_arg)
        if not video.is_absolute():
            video = (wd / video).resolve()
        if not video.is_file():
            err(f"找不到视频文件: {video}")
            return 2
    elif videos:
        log(f"在 {wd} 找到 {len(videos)} 个视频：")
        for i, v in enumerate(videos, 1):
            log(f"  [{i}] {v.name}  ({v.stat().st_size / 1048576:.1f} MB)")
        video = videos[ask_choice("选择视频", len(videos), default=1)]
    else:
        err(f"{wd} 里没有视频文件（支持 {', '.join(VIDEO_EXTS)}）")
        return 2

    try:
        src = ffprobe_media(ffprobe, video)
    except RuntimeError as e:
        err(str(e))
        return 2
    info(f"输入视频: {video.name}")
    log(f"        {src.width}x{src.height}  {src.fps:.3f} fps  {human_time(src.duration)}  "
        f"音轨 {len(src.audio)} 条" + (f"（{','.join(src.audio)}）" if src.audio else ""))
    if src.hdr:
        log(f"        {src.hdr}  {src.color_primaries or '?'}/{src.color_transfer}"
            f"  {src.depth}bit" + ("  Dolby Vision（重编码会丢 DV 动态层）" if src.dovi else ""))

    # ---------- HDR -> SDR ----------
    tonemap_filters = ""
    if args.tonemap:
        if not src.hdr:
            info("源不是 HDR，--tonemap 无效果，忽略")
        else:
            try:
                tonemap_filters, method = pick_tonemap(ffmpeg)
            except RuntimeError as e:
                err(str(e))
                return 2
            info(f"HDR → SDR 色调映射: {method}")

    # ---------- 字幕 ----------
    if args.subtitle:
        sub = Path(args.subtitle)
        if not sub.is_absolute():
            sub = (wd / sub).resolve()
        if not sub.is_file():
            err(f"找不到字幕文件: {sub}")
            return 2
    elif len(subs) == 1:
        sub = subs[0]
    elif subs:
        log(f"找到 {len(subs)} 个字幕：")
        for i, s in enumerate(subs, 1):
            log(f"  [{i}] {s.name}")
        sub = subs[ask_choice("选择字幕", len(subs), default=1)]
    else:
        err(f"{wd} 里没有字幕文件（支持 {', '.join(SUB_EXTS)}）")
        return 2
    info(f"字幕文件: {sub.name}")

    # ---------- 加速方案 ----------
    gpus = detect_gpus()
    log(f"[硬件] 显卡: {' / '.join(gpus) if gpus else '未检测到'}")
    if args.accel == "auto":
        accels = usable_accels(ffmpeg, gpus)
        accel = accels[0]
        if len(accels) > 1 and interactive:
            log("选择加速方案：")
            for i, a in enumerate(accels, 1):
                log(f"  [{i}] {VENDOR_LABEL[a]}" + ("（推荐）" if i == 1 else ""))
            accel = accels[ask_choice("加速方案", len(accels), default=1)]
    else:
        accel = args.accel
        if accel != "cpu" and (accel not in vendors_in(gpus) or not vendor_usable(ffmpeg, accel)):
            warn(f"{VENDOR_LABEL[accel]} 在本机不可用（没用该显卡，或驱动不支持硬件编码）")
            accel = "cpu"
    info(f"加速方案: {VENDOR_LABEL[accel]}")

    # ---------- 编码格式 ----------
    codecs = acceler_encoders(ffmpeg, accel)
    if not codecs:
        err("当前加速方案没有可用编码格式")
        return 2
    hdr_kept = bool(src.hdr and not tonemap_filters)
    if args.codec:
        codec = args.codec
        if codec not in codecs:
            warn(f"{VENDOR_LABEL[accel]} 不支持 {CODEC_LABEL[codec]}，改用 {CODEC_LABEL[codecs[0]]}")
            codec = codecs[0]
    elif len(codecs) == 1:
        codec = codecs[0]
    else:
        codec = "h264" if "h264" in codecs else codecs[0]
        if hdr_kept and "h265" in codecs:
            codec = "h265"                  # HDR 默认给 Main10，比 H.264 靠谱得多
        if interactive:
            log("选择编码格式：")
            for i, c in enumerate(codecs, 1):
                log(f"  [{i}] {CODEC_LABEL[c]}")
            codec = codecs[ask_choice("编码格式", len(codecs), default=codecs.index(codec) + 1)]
    info(f"编码格式: {CODEC_LABEL[codec]}"
         + ("  Main10" if hdr_kept and codec in ("h265", "av1") else ""))
    if hdr_kept:
        if codec == "h264":
            warn("H.264 10bit HDR 兼容性很差（很多电视/播放器不认 High10），建议 -c h265")
        if src.dovi:
            warn("源是 Dolby Vision：重编码后 DV 动态层会丢失，只保留 HDR10 基础层")
        if codec == "h264" and accel in ("amd", "intel"):
            warn(f"{VENDOR_LABEL[accel]} 的 H.264 编码器不支持 10bit，硬件链路大概率回退 CPU")

    # ---------- 画质 ----------
    dq = default_quality_for(codec, accel)
    if args.quality is not None:
        quality = clamp_quality(codec, args.quality)
    elif interactive:
        raw = ask_text(f"画质参数（crf/cq/qp，越小越清晰，默认 {dq}）", str(dq))
        quality = clamp_quality(codec, int(raw)) if raw.isdigit() else dq
    else:
        quality = dq
    info(f"画质参数: {quality}")

    # ---------- 输出路径 ----------
    if args.output:
        out_path = Path(args.output)
        if not out_path.is_absolute():
            out_path = (wd / out_path).resolve()
    else:
        out_path = video.with_name(f"{video.stem}_hardsub_{codec}{pick_output_ext(video, codec)}")
    if out_path.exists() and not args.overwrite and not args.dry_run:
        if not interactive:
            err(f"输出文件已存在: {out_path}（加 --overwrite 覆盖）")
            return 2
        if not ask_yes(f"输出文件已存在，覆盖吗？{out_path.name}", default=False):
            log("已取消。")
            return 1

    # ---------- 组装方案 ----------
    plan = build_plan(ffmpeg, accel, codec, quality, src, tonemapped=bool(tonemap_filters))
    if args.preset and plan.accel == "cpu":
        if "-preset" in plan.enc_params:
            plan.enc_params[plan.enc_params.index("-preset") + 1] = args.preset
        else:
            plan.enc_params += ["-preset", args.preset]

    tmpdir = Path(tempfile.mkdtemp(prefix="hardsub_"))
    try:
        # HDR 片源默认保留 HDR：字幕颜色必须预映射，否则 SDR 白在 PQ 里接近峰值亮度。
        # 这一步是静默的：SDR 片源根本不会走到这里（hdr_transfer 为空）。
        hdr_transfer = (src.color_transfer or "smpte2084") if plan.hdr else ""
        sub_tmp, notes = prepare_subtitle(sub, tmpdir, hdr_transfer)
        for n in notes:
            info(n)
        fonts_dir = (Path(args.fontsdir).resolve() if args.fontsdir
                     else find_fonts_dir(video.parent, sub.parent))
        if fonts_dir:
            info(f"字体目录: {fonts_dir}")
        hdr_sub_style = ""
        if plan.hdr and sub.suffix.lower() in (".srt", ".vtt"):
            hdr_sub_style = hdr_sub_force_style(hdr_transfer)
            info(f"HDR 字幕配色已预映射（{src.hdr or 'HDR'}，"
                 f"SDR 白 ≈ {HDR_SUB_WHITE_NITS:.0f}nit）")

        cmd = build_ffmpeg_cmd(ffmpeg, plan, src, sub_tmp, out_path, fonts_dir,
                               args.threads, "warning", tonemap_filters, hdr_sub_style)
        log("")
        log("=" * 68)
        log(f"  输入    : {video}")
        log(f"  字幕    : {sub}")
        log(f"  输出    : {out_path}")
        log(f"  加速方案: {VENDOR_LABEL[plan.accel]} / {plan.encoder}")
        log(f"  编码参数: {' '.join(plan.enc_params)}")
        log(f"  滤镜链  : {plan.filters}")
        if src.hdr:
            log("  HDR     : " + (f"保留 {src.hdr}（{plan.pix_fmt}）" if plan.hdr
                                  else "已色调映射为 SDR（BT.709 8bit）"))
        log("=" * 68)

        if args.dry_run:
            log("ffmpeg 命令：")
            log("  " + " ".join(c if " " not in c else f'"{c}"' for c in cmd))
            return 0

        if plan.accel != "cpu":
            log("正在试跑 0.5 秒验证硬件链路...")
            good, why = validate_chain(ffmpeg, plan, src, sub_tmp, fonts_dir,
                                       tonemap_filters, hdr_sub_style)
            if good:
                ok("硬件链路正常")
            else:
                warn(f"硬件链路跑不通（{why}）")
                warn("已自动回退 CPU 软编码")
                plan = build_plan(ffmpeg, "cpu", codec, default_quality_for(codec, "cpu"),
                                  src, tonemapped=bool(tonemap_filters))
                cmd = build_ffmpeg_cmd(ffmpeg, plan, src, sub_tmp, out_path, fonts_dir,
                                       args.threads, "warning", tonemap_filters, hdr_sub_style)
                log(f"  改用: {plan.encoder} {' '.join(plan.enc_params)}")

        if not args.yes and sys.stdin.isatty() and not ask_yes("确认开始压制？"):
            log("已取消。")
            return 1

        log("开始压制，按 Ctrl+C 可中断...")
        t0 = time.time()
        code = run_encode(cmd, src.duration, verbose=args.verbose)
        if code != 0:
            err(f"ffmpeg 退出码 {code}，压制失败")
            return code

        elapsed = time.time() - t0
        size_mb = out_path.stat().st_size / 1048576
        used = f"{elapsed:.1f} 秒" if elapsed < 60 else human_time(elapsed)
        log("")
        ok(f"压制完成: {out_path.name}   体积 {size_mb:.1f} MB   耗时 {used}")
        issues = verify_output(ffprobe, out_path, src, hdr_expected=plan.hdr)
        for it in issues:
            warn(it)
        if not issues:
            extra = " / HDR 元数据" if plan.hdr else ""
            ok(f"自检通过：时长 / 分辨率 / 音轨数{extra}全部一致")
        return 0
    finally:
        if args.keep_temp:
            info(f"临时目录: {tmpdir}")
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _pause(argv: list[str]) -> None:
    if {"--no-pause", "--probe", "--list", "--version"} & set(argv):
        return
    if not sys.stdin.isatty():
        return
    try:
        input("按回车键退出...")
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    _argv = sys.argv[1:]
    try:
        _code = main(_argv)
    except SystemExit as e:
        _code = int(e.code or 0)
    except KeyboardInterrupt:
        log("")
        err("已中断")
        _code = 130
    except Exception as e:  # noqa: BLE001
        err(f"未预料的错误: {e}")
        _code = 1
    _pause(_argv)
    sys.exit(_code)
