#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hardsub.py 的自动化测试。

跑法：
    python tests\\test_hardsub.py -v

需要本机有 ffmpeg / ffprobe（优先用 hardsub 自带目录里的）。
最关键的回归测试是 test_first_subtitle_is_burned：
旧的 bat / 直接调 ffmpeg 时，事件数较多的 ASS 会被 libass 吞掉第一条字幕，
这里通过逐帧统计画面亮度来断言"视频开头真的有字幕"。
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hardsub  # noqa: E402

FFMPEG = hardsub.find_tool("ffmpeg")
FFPROBE = hardsub.find_tool("ffprobe")
REAL_ASS = ROOT / "Escaping_the_Minecraft_Underworld_20260922_01 (1).ass"


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def make_video(path: Path, seconds: float = 2.0, size: str = "640x360") -> None:
    """造一个黑底 + 正弦音轨的测试视频（黑底方便统计字幕像素）。"""
    cmd = [FFMPEG, "-y", "-v", "error",
           "-f", "lavfi", "-i", f"color=c=black:s={size}:r=25:d={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "64k", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)


def make_ass(path: Path, count: int = 60) -> None:
    """造一个事件数较多的 ASS（含一条 0.03s 开始的首条事件）。"""
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "WrapStyle: 3",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Arial,60,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,"
        "100,100,0,0,1,3,2,2,10,10,40,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    # 第一条从 0.03s 开始（正是会被吞掉的那条），后续每秒一条
    for i in range(count):
        start = 0.03 + i * 0.03
        end = start + 0.02
        lines.append(f"Dialogue: 0,{fmt_time(start)},{fmt_time(end)},Default,,0,0,0,,LINE-{i + 1}")
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")


def make_srt(path: Path, count: int = 60) -> None:
    out = []
    for i in range(count):
        start = 0.03 + i * 0.03
        end = start + 0.02
        out.append(f"{i + 1}\n{fmt_time(start, srt=True)} --> {fmt_time(end, srt=True)}\nCUE-{i + 1}\n")
    path.write_text("\n".join(out), encoding="utf-8")


def fmt_time(t: float, srt: bool = False) -> str:
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    if srt:
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")
    return f"{h}:{m:02d}:{s:05.2f}"


def brightness_at(video: Path, seconds: float, size: str = "640x360") -> int:
    """统计指定时刻画面下半部分有多少亮像素（>150）。有字幕时远大于 0。"""
    w, h = (int(x) for x in size.split("x"))
    cmd = [FFMPEG, "-v", "error", "-ss", str(seconds), "-i", str(video), "-frames:v", "1",
           "-vf", f"crop={w}:{h // 2}:0:{h // 2},format=gray", "-f", "rawvideo", "-"]
    res = subprocess.run(cmd, capture_output=True)
    return sum(1 for b in res.stdout if b > 150)


def run_main(args: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = hardsub.main(args)
    return code, buf.getvalue()


# --------------------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------------------
class TestPureFunctions(unittest.TestCase):
    def test_escape_filter_path(self):
        got = hardsub.escape_filter_path(r"D:\a b\字幕.ass")
        self.assertEqual(got, "'D\\:/a b/字幕.ass'")
        self.assertTrue(got.startswith("'") and got.endswith("'"))

    def test_escape_filter_path_quote(self):
        self.assertIn("\\'", hardsub.escape_filter_path("/tmp/it's.ass"))

    def test_parse_fps(self):
        self.assertAlmostEqual(hardsub.parse_fps("30000/1001"), 29.97, places=2)
        self.assertEqual(hardsub.parse_fps("25/1"), 25.0)
        self.assertEqual(hardsub.parse_fps("0/0"), 0.0)
        self.assertEqual(hardsub.parse_fps("垃圾"), 0.0)

    def test_gop_for_fps(self):
        self.assertEqual(hardsub.gop_for_fps(25), 50)
        self.assertEqual(hardsub.gop_for_fps(23.976), 48)
        self.assertEqual(hardsub.gop_for_fps(0), 120)

    def test_human_time(self):
        self.assertEqual(hardsub.human_time(65), "01:05")
        self.assertEqual(hardsub.human_time(3725), "1:02:05")
        self.assertEqual(hardsub.human_time(-1), "--:--")

    def test_inject_sacrificial_event_ass(self):
        src = "[Script Info]\nScriptType: v4.00+\n\n[Events]\n" \
              "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n" \
              "Dialogue: 0,0:00:00.03,0:00:00.51,Default,Parrot,0,0,0,,第一条\n" \
              "Dialogue: 0,0:00:00.51,0:00:01.00,Default,Parrot,0,0,0,,第二条\n"
        out, done = hardsub.inject_sacrificial_event(src)
        self.assertTrue(done)
        lines = out.splitlines()
        first = next(l for l in lines if l.startswith("Dialogue:"))
        self.assertIn("0:00:00.00", first)          # 牺牲事件在最前
        self.assertTrue(first.endswith(",,"))       # 空文本
        self.assertIn("第一条", out)                # 原首条仍在
        self.assertEqual(out.count("Dialogue:"), 3)

    def test_inject_sacrificial_event_ssa_format(self):
        src = "[Events]\nFormat: Marked, Start, End, Style, Name, MarginL, MarginR, " \
              "MarginV, Effect, Text\nDialogue: Marked=0,0:00:01.00,0:00:02.00,Default,,0,0,0,,嗨\n"
        out, done = hardsub.inject_sacrificial_event(src)
        self.assertTrue(done)
        first = next(l for l in out.splitlines() if l.startswith("Dialogue:"))
        self.assertIn("0:00:00.00", first)
        self.assertEqual(first.count(","), 9)

    def test_inject_sacrificial_event_no_events_section(self):
        out, done = hardsub.inject_sacrificial_event("[Script Info]\nScriptType: v4.00+\n")
        self.assertFalse(done)
        self.assertIn("ScriptType", out)

    def test_inject_sacrificial_cue_srt(self):
        src = "1\n00:00:00,030 --> 00:00:00,050\n嗨\n"
        out, done = hardsub.inject_sacrificial_cue(src, "srt")
        self.assertTrue(done)
        self.assertIn("00:00:00,000 --> 00:00:00,010", out)
        self.assertLess(out.index("00:00:00,000"), out.index("00:00:00,030"))

    def test_inject_sacrificial_cue_vtt(self):
        src = "WEBVTT\n\n00:00:00.030 --> 00:00:00.050\n嗨\n"
        out, done = hardsub.inject_sacrificial_cue(src, "vtt")
        self.assertTrue(done)
        self.assertTrue(out.startswith("WEBVTT"))
        self.assertIn("00:00:00.000 --> 00:00:00.010", out)

    def test_read_text_any_gbk(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "gbk.ass"
            p.write_bytes("Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,中文测试\n".encode("gbk"))
            text, enc = hardsub.read_text_any(p)
            self.assertEqual(enc, "gb18030")
            self.assertIn("中文测试", text)

    def test_prepare_subtitle_outputs_utf8_and_protects_first_event(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "in.ass"
            body = "\n".join(["Dialogue: 0,0:00:00.03,0:00:00.50,Default,,0,0,0,,一"] * 3)
            src.write_text("[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, "
                           "MarginR, MarginV, Effect, Text\n" + body + "\n", encoding="gbk")
            tmp, notes = hardsub.prepare_subtitle(src, d)
            self.assertTrue(tmp.is_file())
            self.assertIn("一", tmp.read_text(encoding="utf-8"))       # GBK -> UTF-8 且没有乱码
            self.assertTrue(any("首条字幕保护" in n for n in notes))
            self.assertTrue(any("utf-8" in n.lower() for n in notes))
            self.assertEqual(tmp.read_text(encoding="utf-8").count("Dialogue:"), 4)

    def test_pick_output_ext(self):
        self.assertEqual(hardsub.pick_output_ext(Path("a.mp4"), "h264"), ".mp4")
        self.assertEqual(hardsub.pick_output_ext(Path("a.mp4"), "av1"), ".mkv")
        self.assertEqual(hardsub.pick_output_ext(Path("a.webm"), "h264"), ".mkv")
        self.assertEqual(hardsub.pick_output_ext(Path("a.mkv"), "h265"), ".mkv")

    def test_clamp_quality(self):
        self.assertEqual(hardsub.clamp_quality("h264", 23), 23)
        self.assertEqual(hardsub.clamp_quality("h264", 99), 51)
        self.assertEqual(hardsub.clamp_quality("av1", 60), 60)

    def test_scan_dir(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for n in ("a.mp4", "b.mkv", "c.ass", "d.txt", "e.SRT"):
                (d / n).write_text("x")
            videos, subs = hardsub.scan_dir(d)
            self.assertEqual([v.name for v in videos], ["a.mp4", "b.mkv"])
            self.assertEqual(sorted(s.name for s in subs), ["c.ass", "e.SRT"])


# --------------------------------------------------------------------------
# 集成测试（真的调 ffmpeg）
# --------------------------------------------------------------------------
@unittest.skipIf(not (FFMPEG and FFPROBE), "本机没有 ffmpeg / ffprobe")
class TestIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.d = Path(cls.tmp.name)
        make_video(cls.d / "video.mp4")
        make_ass(cls.d / "sub.ass", 60)
        make_srt(cls.d / "sub.srt", 60)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_probe_exit_code(self):
        code, _ = run_main(["--probe"])
        self.assertEqual(code, 0)

    def test_list(self):
        code, out = run_main(["--list", "-d", str(self.d)])
        self.assertEqual(code, 0)
        self.assertIn("video.mp4", out)
        self.assertIn("sub.ass", out)

    def test_first_subtitle_is_burned(self):
        """关键回归：事件数 60 条的 ASS，第一条(0.03s)也必须被烧进画面。"""
        out = self.d / "out_h264.mp4"
        code, txt = run_main(["-d", str(self.d), "-v", "video.mp4", "-s", "sub.ass",
                              "-a", "cpu", "-c", "h264", "-y", "-o", str(out), "--overwrite"])
        self.assertEqual(code, 0, txt)
        self.assertTrue(out.is_file())
        # 0.03~0.05s 的第一条字幕，取 0.04s 那一帧来验证
        self.assertGreater(brightness_at(out, 0.04), 5, "第一条字幕没被烧进画面！")

    def test_srt_first_cue_is_burned(self):
        out = self.d / "out_srt.mp4"
        code, txt = run_main(["-d", str(self.d), "-v", "video.mp4", "-s", "sub.srt",
                              "-a", "cpu", "-c", "h264", "-y", "-o", str(out), "--overwrite"])
        self.assertEqual(code, 0, txt)
        self.assertGreater(brightness_at(out, 0.04), 5, "SRT 第一条字幕没被烧进画面！")

    def test_real_asset_first_subtitle(self):
        """用用户真实那个 14098 条事件的 ASS 复现 + 验证修复。"""
        if not REAL_ASS.is_file():
            self.skipTest("真实 ASS 不在仓库里")
        out = self.d / "out_real.mp4"
        code, txt = run_main(["-d", str(self.d), "-v", "video.mp4", "-s", str(REAL_ASS),
                              "-a", "cpu", "-c", "h264", "-y", "-o", str(out), "--overwrite"])
        self.assertEqual(code, 0, txt)
        self.assertGreater(brightness_at(out, 0.10), 5, "真实字幕第一条被吞了！")

    def test_audio_and_duration_preserved(self):
        out = self.d / "out_keep.mp4"
        code, _ = run_main(["-d", str(self.d), "-v", "video.mp4", "-s", "sub.ass",
                            "-a", "cpu", "-c", "h264", "-y", "-o", str(out), "--overwrite"])
        self.assertEqual(code, 0)
        src = hardsub.ffprobe_media(FFPROBE, self.d / "video.mp4")
        got = hardsub.ffprobe_media(FFPROBE, out)
        self.assertEqual(len(got.audio), len(src.audio))
        self.assertAlmostEqual(got.duration, src.duration, delta=0.5)
        self.assertEqual((got.width, got.height), (src.width, src.height))

    def test_unicode_space_path(self):
        sub_dir = self.d / "中文 目录"
        sub_dir.mkdir(exist_ok=True)
        video = sub_dir / "我的 视频 (1).mp4"
        ass = sub_dir / "我的 字幕 [v2].ass"
        make_video(video)
        make_ass(ass, 60)
        out = sub_dir / "输出 结果.mp4"
        code, txt = run_main(["-v", str(video), "-s", str(ass), "-a", "cpu", "-c", "h264",
                              "-y", "-o", str(out), "--overwrite"])
        self.assertEqual(code, 0, txt)
        self.assertGreater(brightness_at(out, 0.04), 5)
        for junk in sub_dir.iterdir():
            junk.unlink()
        sub_dir.rmdir()

    def test_default_output_name(self):
        code, txt = run_main(["-d", str(self.d), "-v", "video.mp4", "-s", "sub.ass",
                              "-a", "cpu", "-c", "h264", "-y", "--overwrite"])
        self.assertEqual(code, 0, txt)
        made = self.d / "video_hardsub_h264.mp4"
        self.assertTrue(made.is_file())
        made.unlink()

    def test_dry_run_writes_nothing(self):
        out = self.d / "never.mp4"
        code, txt = run_main(["-d", str(self.d), "-v", "video.mp4", "-s", "sub.ass",
                              "-a", "cpu", "-c", "h264", "-y", "-o", str(out), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(out.exists())
        self.assertIn("ffmpeg", txt)

    def test_hw_request_without_hardware_falls_back(self):
        """本机没有 NVIDIA 时必须自动回退 CPU，而不是报错退出。"""
        out = self.d / "fallback.mp4"
        code, txt = run_main(["-d", str(self.d), "-v", "video.mp4", "-s", "sub.ass",
                              "-a", "nvidia", "-c", "h264", "-y", "-o", str(out), "--overwrite"])
        if "nvidia" in hardsub.vendors_in(hardsub.detect_gpus()):
            self.skipTest("本机真有 NVIDIA 显卡")
        self.assertEqual(code, 0, txt)
        self.assertTrue(out.is_file())

    def test_missing_video(self):
        code, _ = run_main(["-d", str(self.d), "-v", "不存在的视频.mp4", "-s", "sub.ass", "-y"])
        self.assertEqual(code, 2)

    def test_missing_subtitle(self):
        code, _ = run_main(["-d", str(self.d), "-v", "video.mp4", "-s", "nope.ass", "-y"])
        self.assertEqual(code, 2)

    def test_empty_dir(self):
        empty = self.d / "empty"
        empty.mkdir(exist_ok=True)
        code, _ = run_main(["-d", str(empty), "-y"])
        self.assertEqual(code, 2)
        empty.rmdir()

    def test_existing_output_needs_overwrite(self):
        out = self.d / "exists.mp4"
        out.write_bytes(b"x")
        code, _ = run_main(["-d", str(self.d), "-v", "video.mp4", "-s", "sub.ass",
                            "-a", "cpu", "-c", "h264", "-y", "-o", str(out)])
        self.assertEqual(code, 2)          # -y 时不允许静默覆盖
        code2, _ = run_main(["-d", str(self.d), "-v", "video.mp4", "-s", "sub.ass",
                             "-a", "cpu", "-c", "h264", "-y", "-o", str(out), "--overwrite"])
        self.assertEqual(code2, 0)
        out.unlink()

    def test_bad_codec_arg(self):
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stderr(io.StringIO()):
                hardsub.main(["-c", "vp9"])
        self.assertEqual(ctx.exception.code, 2)

    def test_bad_accel_arg(self):
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stderr(io.StringIO()):
                hardsub.main(["-a", "cpu2"])
        self.assertEqual(ctx.exception.code, 2)


class TestInteractive(unittest.TestCase):
    """交互式菜单（双击 exe 走的路径）：把 input 换成脚本化回答。"""

    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    @contextlib.contextmanager
    def scripted(self, answers: list[str]):
        from unittest import mock
        idx = {"i": 0}

        def fake_input(prompt: str = "") -> str:
            i = idx["i"]
            idx["i"] += 1
            # 回显一下提问，方便出问题时定位
            print(f"<{prompt.strip()}> -> {answers[i] if i < len(answers) else ''}")
            return answers[i] if i < len(answers) else ""

        with mock.patch.object(sys, "stdin", self._Tty()), \
                mock.patch("builtins.input", fake_input):
            yield

    def test_ask_choice_rejects_bad_input(self):
        with self.scripted(["", "abc", "9", "2"]):
            self.assertEqual(hardsub.ask_choice("选择", 3), 1)

    def test_ask_choice_enter_uses_default(self):
        with self.scripted([""]):
            self.assertEqual(hardsub.ask_choice("选择", 3, default=2), 1)

    def test_ask_yes(self):
        with self.scripted([""]):
            self.assertTrue(hardsub.ask_yes("继续？"))
        with self.scripted(["n"]):
            self.assertFalse(hardsub.ask_yes("继续？"))

    @unittest.skipIf(not (FFMPEG and FFPROBE), "本机没有 ffmpeg / ffprobe")
    def test_interactive_full_flow(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            make_video(d / "甲.mp4")
            make_video(d / "乙.mp4")
            make_ass(d / "字1.ass", 60)
            make_ass(d / "字2.ass", 60)

            # 视频选 1（按码点排序，乙.mp4 在第一位）、字幕选 2、编码选 1、画质回车、确认 y
            with self.scripted(["1", "2", "1", "", "y"]), \
                    mock.patch.object(hardsub, "usable_accels", return_value=["cpu"]):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = hardsub.main(["-d", str(d)])
            self.assertEqual(code, 0, buf.getvalue())
            out = d / "乙_hardsub_h264.mp4"
            self.assertTrue(out.is_file(), buf.getvalue())
            self.assertGreater(brightness_at(out, 0.04), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)