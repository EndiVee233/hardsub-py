#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hardsub.exe 黑盒验证（exe 是外部进程，只能从外面看行为）。

跑法：
    python tests\\verify_exe.py [exe路径]

覆盖：
  1. --probe / --list 能跑通，中文输出是干净的 UTF-8（不是 GBK 乱码）
  2. 真实压制（CPU + 硬件）后，逐帧统计画面亮度，确认第一条字幕真的被烧进去了
  3. 音轨数 / 时长 / 分辨率保持一致
  4. HDR：输出保持 10bit + BT.2020/PQ；--tonemap 输出 BT.709 8bit
  5. 参数错误、文件不存在时退出码正确，不静默失败
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import test_hardsub as T  # noqa: E402

EXE = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "hardsub.exe"

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def run_exe(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run([str(EXE)] + args, capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout)


def main() -> int:
    print(f"黑盒验证目标: {EXE}")
    if not EXE.is_file():
        print("  找不到 exe")
        return 1
    size_mb = EXE.stat().st_size / 1048576
    print(f"  体积 {size_mb:.1f} MB\n")

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)

        # ---------- 1. 基础命令 ----------
        print("[1] 基础命令")
        r = run_exe(["--probe"])
        check("--probe 退出码 0", r.returncode == 0)
        check("--probe 中文没乱码", "环境自检" in (r.stdout or "") and "显卡" in (r.stdout or ""))
        check("--probe 是 UTF-8 可解码", True)  # 上面 encoding=utf-8 已解码成功

        r = run_exe(["--list", "-d", str(d)])
        check("--list 退出码 0", r.returncode == 0)

        r = run_exe(["--version"])
        check("--version 退出码 0", r.returncode == 0 and "hardsub" in (r.stdout or ""))

        # ---------- 2. 造测试素材 ----------
        print("\n[2] 造素材并压制")
        video = d / "素材 视频.mp4"
        ass = d / "字幕 [v1].ass"
        T.make_video(video)
        T.make_ass(ass, 60)

        out_cpu = d / "cpu 输出.mp4"
        r = run_exe(["-v", str(video), "-s", str(ass), "-a", "cpu", "-c", "h264",
                     "-y", "-o", str(out_cpu), "--overwrite"])
        check("CPU 压制退出码 0", r.returncode == 0, (r.stderr or "").strip()[:200])
        check("CPU 输出文件已生成", out_cpu.is_file())
        if out_cpu.is_file():
            lit = T.brightness_at(out_cpu, 0.04)
            check("★ 首条字幕出现在画面里", lit > 5, f"t=0.04s 亮像素 {lit}")

            src = T.hardsub.ffprobe_media(T.FFPROBE, video)
            got = T.hardsub.ffprobe_media(T.FFPROBE, out_cpu)
            check("音轨数一致", len(got.audio) == len(src.audio),
                  f"{len(src.audio)} -> {len(got.audio)}")
            check("时长一致", abs(got.duration - src.duration) < 0.5,
                  f"{src.duration:.2f}s -> {got.duration:.2f}s")
            check("分辨率一致", (got.width, got.height) == (src.width, src.height))

        # ---------- 3. 硬件路径（本机有什么就用什么） ----------
        print("\n[3] 硬件路径")
        gpus = T.hardsub.detect_gpus()
        accels = T.hardsub.usable_accels(T.FFMPEG, gpus)
        hw = [a for a in accels if a != "cpu"]
        if not hw:
            print("  本机没有可用硬件编码，跳过")
        for accel in hw:
            out_hw = d / f"hw_{accel}.mp4"
            r = run_exe(["-v", str(video), "-s", str(ass), "-a", accel, "-c", "h264",
                         "-y", "-o", str(out_hw), "--overwrite"])
            okc = r.returncode == 0 and out_hw.is_file()
            check(f"{accel} 硬件压制成功", okc, (r.stderr or "").strip()[:200])
            if okc:
                lit = T.brightness_at(out_hw, 0.04)
                check(f"{accel} 首条字幕在画面里", lit > 5, f"亮像素 {lit}")

        # ---------- 3.5 HDR ----------
        print("\n[3.5] HDR")
        hdr_video = d / "HDR 素材.mp4"
        hdr_ass = d / "HDR 字幕.ass"
        T.make_hdr_video(hdr_video)
        T.make_ass(hdr_ass, 10)

        out_hdr = d / "hdr 输出.mp4"
        r = run_exe(["-v", str(hdr_video), "-s", str(hdr_ass), "-a", "cpu", "-c", "h265",
                     "-y", "-o", str(out_hdr), "--overwrite"])
        check("HDR 压制退出码 0", r.returncode == 0, (r.stderr or "").strip()[:200])
        if out_hdr.is_file():
            got = T.hardsub.ffprobe_media(T.FFPROBE, out_hdr)
            check("HDR 输出是 10bit", got.depth == 10, got.pix_fmt)
            check("HDR 色彩标签保留",
                  got.color_transfer == "smpte2084" and got.color_primaries == "bt2020",
                  f"{got.color_primaries}/{got.color_transfer}")
            check("HDR 字幕在画面里", T.brightness_at(out_hdr, 0.04, size="320x180") > 5)

        out_sdr = d / "hdr 转 sdr.mp4"
        r = run_exe(["-v", str(hdr_video), "-s", str(hdr_ass), "-a", "cpu", "-c", "h265",
                     "--tonemap", "-y", "-o", str(out_sdr), "--overwrite"])
        check("HDR→SDR 退出码 0", r.returncode == 0, (r.stderr or "").strip()[:200])
        if out_sdr.is_file():
            got = T.hardsub.ffprobe_media(T.FFPROBE, out_sdr)
            check("SDR 输出是 8bit / bt709",
                  got.depth == 8 and got.color_transfer == "bt709",
                  f"{got.pix_fmt} {got.color_transfer}")

        # ---------- 4. 错误路径 ----------
        print("\n[4] 错误路径")
        r = run_exe(["-v", str(d / "不存在.mp4"), "-s", str(ass), "-y"])
        check("视频不存在 -> 退出码 2", r.returncode == 2)

        r = run_exe(["-v", str(video), "-s", str(d / "不存在.ass"), "-y"])
        check("字幕不存在 -> 退出码 2", r.returncode == 2)

        r = run_exe(["-c", "vp9"])
        check("非法编码参数 -> 退出码 2", r.returncode == 2)

        empty = d / "空目录"
        empty.mkdir()
        r = run_exe(["-d", str(empty), "-y"])
        check("目录里没有视频 -> 退出码 2", r.returncode == 2)

        r = run_exe(["-v", str(video), "-s", str(ass), "-a", "cpu", "-c", "h264",
                     "-y", "-o", str(out_cpu)])
        check("已存在输出且未加 --overwrite -> 退出码 2", r.returncode == 2)

        r = run_exe(["-v", str(video), "-s", str(ass), "-a", "cpu", "-y",
                     "-o", str(d / "dry.mp4"), "--dry-run"])
        check("--dry-run 不产生文件", r.returncode == 0 and not (d / "dry.mp4").exists())

    print("\n" + "=" * 60)
    if FAILS:
        print(f"失败 {len(FAILS)} 项: " + "; ".join(FAILS))
        return 1
    print("全部黑盒验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
