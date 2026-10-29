#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hardsub.py 的自动化测试。

跑法：
    python tests\\test_hardsub.py -v

需要本机有 ffmpeg / ffprobe（优先用 hardsub 自带目录里的）。
最关键的回归测试：
  * test_first_subtitle_is_burned：事件数较多的 ASS 不会被 libass 吞掉第一条字幕；
  * TestHdrHelpers / test_hdr_*：HDR 必须保持 10bit + BT.2020/PQ 标签，--tonemap 输出 bt709；
  * TestProgress：ffmpeg 狂刷警告时 stderr 必须被读干净（否则管道写满、进度条卡死）。
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


def make_hdr_video(path: Path, seconds: float = 2.0, size: str = "320x180") -> None:
    """造一个 10bit、BT.2020 / PQ 的 HDR 测试视频（黑底，方便统计字幕像素）。"""
    cmd = [FFMPEG, "-y", "-v", "error",
           "-f", "lavfi", "-i", f"color=c=black:s={size}:r=25:d={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
           "-vf", "format=yuv420p10le,"
                  "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc",
           "-c:v", "libx265", "-x265-params", "log-level=error",
           "-pix_fmt", "yuv420p10le",
           "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
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


def max_luma10(video: Path, seconds: float, size: str = "320x180") -> int:
    """取一帧 10bit Y 平面里最亮的码值（用于核对 HDR 字幕白是不是 203nit）。"""
    w, h = (int(x) for x in size.split("x"))
    cmd = [FFMPEG, "-v", "error", "-ss", str(seconds), "-i", str(video), "-frames:v", "1",
           "-f", "rawvideo", "-pix_fmt", "yuv420p10le", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    return max(int.from_bytes(raw[i:i + 2], "little") for i in range(0, w * h * 2, 2))


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


class TestHdrHelpers(unittest.TestCase):
    """HDR 相关纯函数（不调 ffmpeg 的部分）。"""

    def test_pix_fmt_depth(self):
        cases = {"yuv420p": 8, "nv12": 8, "yuv420p10le": 10, "p010le": 10,
                 "yuv444p12le": 12, "gray": 8, "": 8}
        for fmt, depth in cases.items():
            self.assertEqual(hardsub.pix_fmt_depth(fmt), depth, fmt)

    def test_clean_tag(self):
        self.assertEqual(hardsub.clean_tag("bt2020"), "bt2020")
        self.assertEqual(hardsub.clean_tag("unknown"), "")
        self.assertEqual(hardsub.clean_tag("reserved"), "")
        self.assertEqual(hardsub.clean_tag(None), "")

    def test_hdr_filter_chain_cpu_keeps_10bit(self):
        chain = hardsub.build_filters("subtitles='x.ass'", "cpu", "yuv420p10le")
        self.assertTrue(chain.endswith("format=yuv420p10le"))
        self.assertNotIn("hwdownload", chain)

    def test_hdr_filter_chain_hw_downloads_p010(self):
        chain = hardsub.build_filters("subtitles='x.ass'", "nvidia", "p010le", "", "p010le")
        self.assertIn("hwdownload,format=p010le", chain)
        self.assertNotIn("nv12", chain)
        self.assertIn("hwupload_cuda", chain)

    def test_sdr_filter_chain_hw_uses_nv12(self):
        chain = hardsub.build_filters("subtitles='x.ass'", "nvidia", "nv12", "", "nv12")
        self.assertIn("format=nv12", chain)

    def test_tonemap_runs_before_subtitles(self):
        """字幕必须在 tonemap 之后烧，否则字幕会被一起压暗。"""
        chain = hardsub.build_filters("subtitles='x.ass'", "nvidia", "nv12",
                                      hardsub.TONEMAP_ZSCALE, "p010le")
        self.assertTrue(chain.startswith("hwdownload,format=p010le,zscale="))
        self.assertLess(chain.index("tonemap=tonemap=hable"), chain.index("subtitles="))

    def test_color_opts(self):
        media = hardsub.Media(path=Path("x"), color_primaries="bt2020",
                              color_transfer="smpte2084", color_space="bt2020nc")
        hdr_opts = hardsub.color_opts_for(media, True, False)
        self.assertIn("bt2020", hdr_opts)
        self.assertIn("smpte2084", hdr_opts)
        self.assertIn("bt2020nc", hdr_opts)
        sdr_opts = hardsub.color_opts_for(None, False, True)
        self.assertIn("bt709", sdr_opts)
        self.assertEqual(hardsub.color_opts_for(None, False, False), [])

    def test_master_display_param(self):
        side = {"red_x": "34000/50000", "red_y": "16000/50000",
                "green_x": "13250/50000", "green_y": "34500/50000",
                "blue_x": "7500/50000", "blue_y": "3000/50000",
                "white_point_x": "15635/50000", "white_point_y": "16450/50000",
                "min_luminance": "50/10000", "max_luminance": "10000000/10000"}
        self.assertEqual(
            hardsub.master_display_param(side),
            "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)")
        self.assertEqual(hardsub.master_display_param({}), "")

    @unittest.skipIf(not FFMPEG, "本机没有 ffmpeg")
    def test_plan_hdr(self):
        media = hardsub.Media(path=Path("x"), depth=10, hdr="HDR10（PQ）",
                              color_primaries="bt2020", color_transfer="smpte2084",
                              color_space="bt2020nc")
        plan = hardsub.build_plan(FFMPEG, "cpu", "h265", 23, media)
        self.assertTrue(plan.hdr)
        self.assertEqual(plan.pix_fmt, "yuv420p10le")
        self.assertIn("-x265-params", plan.enc_params)
        self.assertTrue(any("hdr-opt=1" in p for p in plan.enc_params))

        if hardsub.encoder_available(FFMPEG, "hevc_nvenc"):
            hw = hardsub.build_plan(FFMPEG, "nvidia", "h265", 23, media)
            self.assertEqual(hw.pix_fmt, "p010le")
            self.assertEqual(hw.hw_pix_fmt, "p010le")
            self.assertIn("main10", hw.enc_params)

        sdr = hardsub.build_plan(FFMPEG, "cpu", "h265", 23, media, tonemapped=True)
        self.assertFalse(sdr.hdr)
        self.assertEqual(sdr.pix_fmt, "yuv420p")
        self.assertIn("bt709", sdr.color_opts)


class TestHdrSubtitleColor(unittest.TestCase):
    """HDR 字幕配色：SDR 白必须映射成 203nit，而不是 PQ 峰值附近的 9900nit。"""

    def test_pq_white_maps_to_203nit_code(self):
        # 203nit 的 PQ 信号 ≈ 0.58 -> 148/255，实测烧进视频后约 201nit
        self.assertEqual(hardsub.map_sdr_color_to_hdr((255, 255, 255), "smpte2084"),
                         (148, 148, 148))

    def test_hlg_white_maps_to_75_percent_signal(self):
        # HLG 0.75 信号 = 1000nit 屏上的 203nit
        self.assertEqual(hardsub.map_sdr_color_to_hdr((255, 255, 255), "arib-std-b67"),
                         (191, 191, 191))

    def test_black_stays_black(self):
        for transfer in ("smpte2084", "arib-std-b67"):
            self.assertEqual(hardsub.map_sdr_color_to_hdr((0, 0, 0), transfer), (0, 0, 0))

    def test_color_mapping_is_not_identity_but_keeps_channels_distinct(self):
        rgb = hardsub.map_sdr_color_to_hdr((255, 0, 0), "smpte2084")
        self.assertNotEqual(rgb, (255, 0, 0))
        self.assertGreater(rgb[0], rgb[1])
        self.assertGreater(rgb[1], rgb[2])

    def test_ass_color_parse_and_format(self):
        self.assertEqual(hardsub.parse_ass_color("&H00FFFFFF"),
                         (255, 255, 255, 0, 8))
        self.assertEqual(hardsub.parse_ass_color("&H0000FF"),
                         (255, 0, 0, 0, 6))
        self.assertEqual(hardsub.parse_ass_color("&H80000000"),
                         (0, 0, 0, 0x80, 8))
        self.assertEqual(hardsub.format_ass_color((148, 148, 148), 0, 8), "&H00949494")
        self.assertEqual(hardsub.format_ass_color((148, 148, 148), 0, 6), "&H949494")
        self.assertIsNone(hardsub.parse_ass_color("not-a-color"))

    def test_remap_ass_colors_style_and_inline_overrides(self):
        text = "\n".join([
            "[Script Info]",
            "ScriptType: v4.00+",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding",
            "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
            "0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            r"Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,{\c&HFFFFFF&}white "
            r"{\1c&H0000FF&}red",
        ])
        out, changed = hardsub.remap_ass_colors(text, "smpte2084")
        self.assertGreaterEqual(changed, 3)
        self.assertIn("&H00949494", out)                 # 主色白
        self.assertIn("&H80000000", out)                 # 半透明黑背板保持不变
        self.assertIn(r"{\c&H949494&}", out)             # 内联白
        self.assertIn(r"{\1c&H385388&}", out)            # 内联红（BGR 顺序）
        self.assertIn("&H00385388", out)                 # 样式里的红色副色也映射了

    def test_sdr_subtitle_is_not_remapped(self):
        text = "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000," \
               "0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1"
        out, changed = hardsub.remap_ass_colors(text, "")
        self.assertEqual(changed, 0)
        self.assertEqual(out, text)

    def test_peak_luma_hits_1000nit_for_any_color(self):
        """卡拉OK高亮色按色相保留、亮度归一到 1000nit（不是按原色亮度变暗）。"""
        def nits(code: int) -> float:
            e = code / 255.0
            m1, m2 = 0.1593017578125, 78.84375
            c1, c2, c3 = 0.8359375, 18.8515625, 18.6875
            p = e ** (1 / m2)
            return ((max(p - c1, 0) / (c2 - c3 * p)) ** (1 / m1)) * 10000.0

        for rgb in ((255, 0, 255), (255, 199, 142), (0, 128, 255)):
            mapped = hardsub.map_sdr_color_to_hdr(rgb, "smpte2084", peak_luma=True)
            r, g, b = (nits(c) for c in mapped)
            luma = 0.2627 * r + 0.6780 * g + 0.0593 * b
            self.assertAlmostEqual(luma, hardsub.HDR_HIGHLIGHT_NITS, delta=25.0,
                                   msg=f"{rgb} -> {mapped}")
        # 白色顶到 1000nit；黑色仍是黑
        self.assertEqual(hardsub.map_sdr_color_to_hdr((255, 255, 255), "smpte2084", True),
                         (192, 192, 192))
        self.assertEqual(hardsub.map_sdr_color_to_hdr((0, 0, 0), "smpte2084", True),
                         (0, 0, 0))

    def test_highlight_spans_finds_word_and_ignores_unreset_color(self):
        line = (r"Dialogue: 0,0:00:01.11,0:00:01.19,Default,Spoke,0,0,0,,This is "
                r"{\c&HFEFFFE&\3c&HFF00FF&\3a&H40&\blur12.0}the{\c\3c\3a\blur} Unstable")
        spans = hardsub.highlight_spans(line)
        self.assertEqual(len(spans), 1)
        start, end = spans[0]
        self.assertIn("the", line[start:end])
        self.assertNotIn("Unstable", line[start:end])
        self.assertEqual(hardsub.highlight_spans(
            r"Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{\c&HFF0000&}red"), [])
        two = (r"Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,"
               r"{\c&HFF0000&}one{\c} {\c&H00FF00&}two{\c}")
        self.assertEqual(len(hardsub.highlight_spans(two)), 2)

    def test_karaoke_highlight_peaks_only_in_default_style(self):
        text = "\n".join([
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding",
            "Style: Default,Comic Sans MS,53,&H00828282,&H0000FFFF,&H00000000,"
            "&H00000000,-1,0,0,0,100,100,0,0,1,3,3,2,20,20,120,1",
            "Style: 中文字幕,猫啃什锦黑,58,&H0000FFFF,&H0000FFFF,&H00000000,"
            "&H00000000,-1,0,0,0,100,100,0,0,1,3.0,2,2,10,10,125,1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            r"Dialogue: 0,0:00:01.11,0:00:01.19,Default,Spoke,0,0,0,,This is "
            r"{\c&HFEFFFE&\3c&HFF00FF&\3a&H40&\blur12.0}the{\c\3c\3a\blur} SMP",
            r"Dialogue: 0,0:00:01.11,0:00:01.19,中文字幕,Spoke,0,0,0,,"
            r"{\c&HFF00FF&}中文",
        ])
        out, _ = hardsub.remap_ass_colors(text, "smpte2084")
        self.assertIn(r"{\c&HC0C0C0&\3c&HE09DD8&\3a&H40&\blur12.0}the{\c\3c\3a\blur}",
                      out)
        # 样式里的灰字仍按普通映射（不是高亮色），复位块保持无参数
        self.assertIn("&H006E6E6E", out)
        # 其他样式里的内联颜色只做普通映射
        self.assertIn(r"{\c&H92578A&}中文", out)

        normal, _ = hardsub.remap_ass_colors(text, "smpte2084", karaoke_peak=False)
        self.assertIn(r"\3c&H92578A", normal)
        self.assertNotIn(r"\3c&HE09DD8", normal)

    def test_karaoke_peak_can_be_disabled_in_prepare(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "k.ass"
            src.write_text(
                "[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
                "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
                "MarginL, MarginR, MarginV, Encoding\n"
                "Style: Default,Arial,20,&H00828282,&H00FFFFFF,&H00000000,&H00000000,"
                "0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n"
                "\n[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
                "MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,"
                "{\\c&HFF00FF&\\3c&H00FFFF&}hi{\\c\\3c}\n",
                encoding="utf-8")
            peak, _ = hardsub.prepare_subtitle(src, Path(d), "smpte2084")
            peak_text = peak.read_text(encoding="utf-8")
            plain, _ = hardsub.prepare_subtitle(src, Path(d), "smpte2084", karaoke_peak=False)
            self.assertIn("E09DD8", peak_text)
            self.assertNotIn("E09DD8", plain.read_text(encoding="utf-8"))

    def test_srt_force_style_uses_hdr_white(self):
        style = hardsub.hdr_sub_force_style("smpte2084")
        self.assertIn("PrimaryColour=&H00949494", style)
        self.assertIn("OutlineColour=&H00000000", style)
        hlg = hardsub.hdr_sub_force_style("arib-std-b67")
        self.assertIn("PrimaryColour=&H00BFBFBF", hlg)

    def test_sub_filter_opts_adds_force_style_only_when_given(self):
        plain = hardsub.sub_filter_opts(Path("x.srt"), None)
        self.assertNotIn("force_style", plain)
        styled = hardsub.sub_filter_opts(Path("x.srt"), None,
                                         hardsub.hdr_sub_force_style("smpte2084"))
        self.assertIn("force_style='PrimaryColour=&H00949494", styled)

    def test_prepare_subtitle_maps_colors_only_for_hdr(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.ass"
            src.write_text(
                "[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
                "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
                "MarginL, MarginR, MarginV, Encoding\n"
                "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
                "0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n"
                "\n[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
                "MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,hi\n",
                encoding="utf-8")
            plain, _ = hardsub.prepare_subtitle(src, Path(d))
            self.assertIn("&H00FFFFFF", plain.read_text(encoding="utf-8"))
            hdr, notes = hardsub.prepare_subtitle(src, Path(d), "smpte2084")
            self.assertIn("&H00949494", hdr.read_text(encoding="utf-8"))
            self.assertTrue(any("HDR" in n for n in notes))


class TestProgress(unittest.TestCase):
    """进度解析 + stderr 处理（对应'视频在编、进度条不动'的 bug）。"""

    def test_parse_progress_time(self):
        self.assertAlmostEqual(hardsub.parse_progress_time("out_time_us", "1500000"), 1.5)
        # out_time_ms 在 ffmpeg 里其实也是微秒（历史遗留），按微秒算才对
        self.assertAlmostEqual(hardsub.parse_progress_time("out_time_ms", "1500000"), 1.5)
        self.assertAlmostEqual(hardsub.parse_progress_time("out_time", "00:01:02.500000"), 62.5)
        self.assertAlmostEqual(hardsub.parse_progress_time("out_time", "01:00:00.000000"), 3600.0)
        self.assertIsNone(hardsub.parse_progress_time("out_time_us", "N/A"))
        self.assertIsNone(hardsub.parse_progress_time("out_time", "N/A"))

    def test_pick_error_line_prefers_root_cause(self):
        stderr = ("[out#0/null @ 0x1] Nothing was written into output file, because...\n"
                  "[hwdownload @ 0x2] Invalid output format nv12 for hwframe download.\n")
        self.assertIn("Invalid output format nv12", hardsub.pick_error_line(stderr))

    def test_run_encode_drains_stderr(self):
        """
        旧实现把 stderr 直接继承/不读：ffmpeg 警告一多，管道写满就卡死，
        表现正好是"视频还在压、进度条不动"。这里让子进程狂写 20000 行警告。
        """
        script = (
            "import os, sys, threading\n"
            "_t = threading.Timer(30, lambda: os._exit(9))\n"
            "_t.daemon = True\n"
            "_t.start()\n"
            "for i in range(20000):\n"
            "    sys.stderr.write('warning line %d\\n' % i)\n"
            "sys.stderr.flush()\n"
            "for i in range(1, 11):\n"
            "    sys.stdout.write('out_time_us=%d\\n' % (i * 1000000))\n"
            "    sys.stdout.write('progress=continue\\n')\n"
            "sys.stdout.flush()\n"
            "sys.stdout.write('out_time_us=10000000\\nprogress=end\\n')\n"
            "sys.stdout.flush()\n"
        )
        code = hardsub.run_encode([sys.executable, "-c", script], 10.0)
        self.assertEqual(code, 0)

    def test_run_encode_reports_warnings(self):
        script = ("import sys\n"
                  "sys.stderr.write('[x @ 0x1] something odd happened\\n')\n"
                  "sys.stdout.write('progress=end\\n')\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = hardsub.run_encode([sys.executable, "-c", script], 10.0)
        self.assertEqual(code, 0)
        self.assertIn("something odd happened", buf.getvalue())

    def test_run_encode_shows_failure_detail(self):
        script = ("import sys\n"
                  "sys.stdout.write('progress=end\\n')\n"
                  "sys.stderr.write('[dec @ 0x1] Invalid data found when processing input\\n')\n"
                  "sys.exit(3)\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = hardsub.run_encode([sys.executable, "-c", script], 10.0)
        self.assertEqual(code, 3)
        self.assertIn("Invalid data found", buf.getvalue())


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
        make_hdr_video(cls.d / "hdr.mp4")
        make_ass(cls.d / "hdr.ass", 10)

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

    def test_hdr_video_keeps_10bit_and_tags(self):
        """HDR 源必须输出 10bit + BT.2020/PQ 标签，且字幕正常烧进去。"""
        out = self.d / "out_hdr.mp4"
        code, txt = run_main(["-d", str(self.d), "-v", "hdr.mp4", "-s", "hdr.ass",
                              "-a", "cpu", "-c", "h265", "-y", "-o", str(out), "--overwrite"])
        self.assertEqual(code, 0, txt)
        src = hardsub.ffprobe_media(FFPROBE, self.d / "hdr.mp4")
        got = hardsub.ffprobe_media(FFPROBE, out)
        self.assertEqual(src.hdr, "HDR10（PQ）")
        self.assertEqual(got.depth, 10)
        self.assertEqual(got.color_transfer, src.color_transfer)
        self.assertEqual(got.color_primaries, src.color_primaries)
        # 字幕预映射后白字是 203nit（10bit Y≈572），不再是过曝的 939
        self.assertGreater(max_luma10(out, 0.04), 500)

    def test_hdr_subtitle_white_is_about_203nit(self):
        """HDR 白字幕必须预映射到 203nit（10bit Y ≈ 572），而不是 PQ 峰值（≈939）。"""
        out = self.d / "out_hdr_subcolor.mp4"
        code, txt = run_main(["-d", str(self.d), "-v", "hdr.mp4", "-s", "hdr.ass",
                              "-a", "cpu", "-c", "h265", "-y", "-o", str(out), "--overwrite"])
        self.assertEqual(code, 0, txt)
        y = max_luma10(out, 0.04)
        self.assertGreater(y, 500, f"字幕白太暗（Y={y}）")
        self.assertLess(y, 640, f"字幕白过曝了（Y={y}，未做 HDR 配色映射？）")

    def test_hdr_tonemap_to_sdr(self):
        """--tonemap 必须输出 BT.709 8bit，字幕照样在。"""
        if not {"zscale", "tonemap"} <= hardsub.list_filters(FFMPEG):
            self.skipTest("ffmpeg 没有 zscale/tonemap")
        out = self.d / "out_hdr_sdr.mp4"
        code, txt = run_main(["-d", str(self.d), "-v", "hdr.mp4", "-s", "hdr.ass",
                              "-a", "cpu", "-c", "h265", "--tonemap", "-y",
                              "-o", str(out), "--overwrite"])
        self.assertEqual(code, 0, txt)
        got = hardsub.ffprobe_media(FFPROBE, out)
        self.assertEqual(got.depth, 8)
        self.assertEqual(got.color_transfer, "bt709")
        self.assertGreater(brightness_at(out, 0.04, size="320x180"), 5)

    def test_hdr_hardware_path(self):
        """有硬件时，HDR 也必须走硬件链路成功（旧的 nv12 链会直接报错回退 CPU）。"""
        accels = [a for a in hardsub.usable_accels(FFMPEG, hardsub.detect_gpus()) if a != "cpu"]
        if not accels:
            self.skipTest("本机没有硬件编码")
        for accel in accels:
            if "h265" not in hardsub.acceler_encoders(FFMPEG, accel):
                continue
            out = self.d / f"out_hdr_{accel}.mp4"
            code, txt = run_main(["-d", str(self.d), "-v", "hdr.mp4", "-s", "hdr.ass",
                                  "-a", accel, "-c", "h265", "-y", "-o", str(out),
                                  "--overwrite"])
            self.assertEqual(code, 0, txt)
            got = hardsub.ffprobe_media(FFPROBE, out)
            self.assertEqual(got.depth, 10, txt)

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