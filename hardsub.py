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
  5. 进度条 + 压制后自动校验（时长 / 分辨率 / 音轨数）。
  6. 全参数化命令行，可被其它脚本调度。

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
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "1.0.0"

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


def parse_fps(text: str) -> float:
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(text)
    except (ValueError, ZeroDivisionError):
        return 0.0


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


def prepare_subtitle(src: Path, workdir: Path) -> tuple[Path, list[str]]:
    """
    把字幕复制到工作目录，并做两件事：
      1. 统一转成 UTF-8（GBK 老字幕也能被 libass 正确渲染）
      2. 注入"牺牲事件"，规避第一条字幕被吞的 bug
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


def build_filters(sub_filter: str, accel: str) -> str:
    if accel == "cpu":
        return f"{sub_filter},format=yuv420p"
    tail = {"nvidia": "format=nv12,hwupload_cuda",
            "amd": "format=nv12,hwupload=derive_device=d3d11va",
            "intel": "format=nv12,hwupload=derive_device=qsv"}[accel]
    return f"hwdownload,{sub_filter},{tail}"


def build_plan(ffmpeg: str, accel: str, codec: str, quality: int) -> Plan:
    if accel == "cpu":
        encoder = pick_cpu_encoder(ffmpeg, codec)
        if not encoder:
            raise RuntimeError(f"这个 ffmpeg 没有可用的 {codec} 软编码器")
        params = ["-crf", str(quality)]
        # libsvtav1 的 preset 是数字(0-13)，写 "medium" 会直接报错
        params += ["-preset", "8" if encoder == "libsvtav1" else "medium"]
        return Plan("cpu", codec, encoder, [], params, quality)

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
    return Plan(accel, codec, encoder, accel_input_opts(accel), params, quality)


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


def sub_filter_opts(sub_tmp: Path, fonts_dir: Path | None) -> str:
    s = "subtitles=" + escape_filter_path(sub_tmp)
    if fonts_dir:
        s += ":fontsdir=" + escape_filter_path(fonts_dir)
    return s


def build_ffmpeg_cmd(ffmpeg: str, plan: Plan, media: Media, sub_tmp: Path, out_path: Path,
                     fonts_dir: Path | None, threads: int, loglevel: str) -> list[str]:
    plan.filters = build_filters(sub_filter_opts(sub_tmp, fonts_dir), plan.accel)

    cmd = [ffmpeg, "-hide_banner", "-loglevel", loglevel, "-y", "-nostdin"]
    cmd += plan.input_opts
    cmd += ["-i", str(media.path), "-map", f"0:{media.v_index}"]
    if media.audio:
        cmd += ["-map", "0:a?"]
    cmd += ["-map_metadata", "0", "-vf", plan.filters, "-c:v", plan.encoder]
    cmd += plan.enc_params
    cmd += ["-g", str(gop_for_fps(media.fps))]
    if plan.accel == "cpu":
        cmd += ["-pix_fmt", "yuv420p"]      # 硬件链路不能加，否则会把 hw 帧拉回内存
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


def validate_chain(ffmpeg: str, plan: Plan, media: Media, sub_tmp: Path,
                   fonts_dir: Path | None) -> tuple[bool, str]:
    """
    正式压制前用真实视频试跑 0.5 秒整条链路（硬解 + 滤镜 + 编码）。
    硬件方案常常"能列出编码器但跑不动"，提前 0.5 秒验证可以避免白等一小时。
    """
    filters = build_filters(sub_filter_opts(sub_tmp, fonts_dir), plan.accel)
    cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y"]
    cmd += plan.input_opts
    cmd += ["-i", str(media.path), "-t", "0.5", "-an", "-vf", filters,
            "-c:v", plan.encoder] + plan.enc_params + ["-f", "null", "-"]
    res = run_capture(cmd)
    if res.returncode == 0:
        return True, ""
    tail = [ln for ln in (res.stderr or "").strip().splitlines() if ln.strip()]
    return False, tail[-1] if tail else "未知错误"


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


def run_encode(cmd: list[str], duration: float) -> int:
    """跑 ffmpeg，解析 -progress 输出画进度条。"""
    tty = sys.stdout.isatty()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=None,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    start = time.time()
    last_report = 0.0
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and value.strip().isdigit() and duration > 0:
                    done = int(value) / 1_000_000
                    pct = max(0.0, min(1.0, done / duration))
                    speed = done / max(0.001, time.time() - start)
                    eta = (duration - done) / speed if speed > 0 else -1
                    if tty:
                        width = 32
                        filled = int(width * pct)
                        bar = "█" * filled + "░" * (width - filled)
                        sys.stdout.write(f"\r  {bar} {pct * 100:5.1f}%  "
                                         f"剩余 {human_time(eta)}  ({human_time(done)}/{human_time(duration)})")
                        sys.stdout.flush()
                    elif time.time() - last_report > 5:
                        last_report = time.time()
                        log(f"  进度 {pct * 100:5.1f}%  剩余 {human_time(eta)}")
                elif key == "progress" and value.strip() == "end":
                    break
    finally:
        if tty:
            sys.stdout.write("\r" + " " * 78 + "\r")
            sys.stdout.flush()
        code = proc.wait()
    return code


def verify_output(ffprobe: str, out_path: Path, src: Media) -> list[str]:
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
    if args.codec:
        codec = args.codec
        if codec not in codecs:
            warn(f"{VENDOR_LABEL[accel]} 不支持 {CODEC_LABEL[codec]}，改用 {CODEC_LABEL[codecs[0]]}")
            codec = codecs[0]
    elif len(codecs) == 1:
        codec = codecs[0]
    else:
        codec = "h264" if "h264" in codecs else codecs[0]
        if interactive:
            log("选择编码格式：")
            for i, c in enumerate(codecs, 1):
                log(f"  [{i}] {CODEC_LABEL[c]}")
            codec = codecs[ask_choice("编码格式", len(codecs), default=codecs.index(codec) + 1)]
    info(f"编码格式: {CODEC_LABEL[codec]}")

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
    plan = build_plan(ffmpeg, accel, codec, quality)
    if args.preset and plan.accel == "cpu":
        if "-preset" in plan.enc_params:
            plan.enc_params[plan.enc_params.index("-preset") + 1] = args.preset
        else:
            plan.enc_params += ["-preset", args.preset]

    tmpdir = Path(tempfile.mkdtemp(prefix="hardsub_"))
    try:
        sub_tmp, notes = prepare_subtitle(sub, tmpdir)
        for n in notes:
            info(n)
        fonts_dir = (Path(args.fontsdir).resolve() if args.fontsdir
                     else find_fonts_dir(video.parent, sub.parent))
        if fonts_dir:
            info(f"字体目录: {fonts_dir}")

        cmd = build_ffmpeg_cmd(ffmpeg, plan, src, sub_tmp, out_path, fonts_dir,
                               args.threads, "warning")
        log("")
        log("=" * 68)
        log(f"  输入    : {video}")
        log(f"  字幕    : {sub}")
        log(f"  输出    : {out_path}")
        log(f"  加速方案: {VENDOR_LABEL[plan.accel]} / {plan.encoder}")
        log(f"  编码参数: {' '.join(plan.enc_params)}")
        log(f"  滤镜链  : {plan.filters}")
        log("=" * 68)

        if args.dry_run:
            log("ffmpeg 命令：")
            log("  " + " ".join(c if " " not in c else f'"{c}"' for c in cmd))
            return 0

        if plan.accel != "cpu":
            log("正在试跑 0.5 秒验证硬件链路...")
            good, why = validate_chain(ffmpeg, plan, src, sub_tmp, fonts_dir)
            if good:
                ok("硬件链路正常")
            else:
                warn(f"硬件链路跑不通（{why}）")
                warn("已自动回退 CPU 软编码")
                plan = build_plan(ffmpeg, "cpu", codec, default_quality_for(codec, "cpu"))
                cmd = build_ffmpeg_cmd(ffmpeg, plan, src, sub_tmp, out_path, fonts_dir,
                                       args.threads, "warning")
                log(f"  改用: {plan.encoder} {' '.join(plan.enc_params)}")

        if not args.yes and sys.stdin.isatty() and not ask_yes("确认开始压制？"):
            log("已取消。")
            return 1

        log("开始压制，按 Ctrl+C 可中断...")
        t0 = time.time()
        code = run_encode(cmd, src.duration)
        if code != 0:
            err(f"ffmpeg 退出码 {code}，压制失败")
            return code

        elapsed = time.time() - t0
        size_mb = out_path.stat().st_size / 1048576
        used = f"{elapsed:.1f} 秒" if elapsed < 60 else human_time(elapsed)
        log("")
        ok(f"压制完成: {out_path.name}   体积 {size_mb:.1f} MB   耗时 {used}")
        issues = verify_output(ffprobe, out_path, src)
        for it in issues:
            warn(it)
        if not issues:
            ok("自检通过：时长 / 分辨率 / 音轨数全部一致")
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
