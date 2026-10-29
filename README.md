# hardsub — 智能硬编码字幕（命令行版）

把 `.ass` / `.ssa` / `.srt` / `.vtt` 字幕**永久烧进视频画面**（硬字幕），
自动挑 NVIDIA / AMD / Intel 硬件编码；硬件跑不通会自动回退 CPU 软编码。

- 🧩 单文件 exe，免装 Python → [**下载最新版**](../../releases/latest)
- 🐍 纯标准库，无第三方依赖（`hardsub.py` 可直接 `python hardsub.py` 运行）
- 🖥️ 交互式菜单 + 全参数化命令行，能挂到脚本里批量跑

> 依赖 **ffmpeg / ffprobe**：放到 `hardsub.exe` 同目录，或加入系统 `PATH`。
> 用 `hardsub.exe --probe` 可先看一眼本机支持哪些硬件编码。

---

## 它解决的坑

这个工具是从一个 300 行的 cmd 脚本重写来的，顺手修掉了几类"压到一半才发现"的问题：

| 旧 cmd 脚本 | 本工具 |
| --- | --- |
| **字幕文件里第一条字幕会被吞掉**（事件数多时 ffmpeg/libass 的行为） | 在 ASS 头部注入一条空"牺牲事件"，首条字幕稳在；有回归测试守着 |
| 路径 / 文件名里不能有 `!`（cmd 延迟展开会吃字符） | 中文、空格、括号、`!` 全部安全 |
| `-acodec copy` 只保留一条音轨，装不下就整个失败 | 保留全部音轨；与 mp4 不兼容时自动转 AAC |
| 只看 `ffmpeg -encoders` 列表猜"支持硬件编码" | **真编 3 帧**验证驱动可用，再用真实视频试跑 0.5 秒整条链路 |
| 硬件链路失败 → 白跑到一半报错 | 试跑失败自动回退 CPU，并告诉你原因 |
| CPU 路径也带 `hwdownload`，软解时可能直接报错 | CPU 路径干净，不依赖硬件帧 |
| `libsvtav1` 配 `-preset medium` → 必报错 | 按编码器给正确参数 |
| **HDR 视频** 被压成 8bit，或硬件链路直接报 `Invalid output format nv12 for hwframe download` | 自动识别 HDR10 / HLG，全程 10bit（`yuv420p10le` / `p010le`），写回 BT.2020 + PQ/HLG 标签和 HDR10 静态元数据；HDR 源静默走 HDR 链路，普通源走普通链路；字幕颜色按 203nit 预映射，不会白得刺眼；`--tonemap` 可转 SDR |
| 进度条在有些情况下不动（ffmpeg 警告塞满 stderr 管道 / 旧版 ffmpeg 没有 `out_time_us`） | stdout、stderr 各一个读取线程；进度字段三级回退；长时间没进度会提示 `[等待 ffmpeg…]`；结束时汇总警告 |
| 没有进度、压完不知道对不对 | 进度条 + 压制后自检（时长 / 分辨率 / 音轨数 / HDR 元数据） |
| 只能人工交互 | 全参数化，可脚本化调度 |

### 第一条字幕为什么会被吞

实测（把字幕烧到黑底视频上逐帧统计亮像素）：

| ASS 事件条数 | 成片里字幕首次出现 |
| --- | --- |
| ≤ 50 条 | 0.04s，正常 |
| ≥ 100 条 | 0.52s —— 文件里第一条事件没了 |

加 `Collisions: Normal`、删 `WrapStyle`、把首条时间改成 `0.00` 都无效。
**有效做法**：在 `[Events]` 的 `Format:` 行后插一条"空文本 + 10ms"的事件当垫背，
被吞的是它，真实首条字幕照常渲染（事件少时插进去也毫无副作用）。
实现见 `hardsub.py` 的 `inject_sacrificial_event()`。

---

## 快速开始

```bash
# 0) 先自检：ffmpeg 在哪、本机哪些硬件编码真能跑
hardsub.exe --probe

# 1) 什么都不带 = 交互式菜单（双击 exe 也是这个效果）
hardsub.exe

# 2) 全自动：AMD 硬编 H.265，画质 23，不问任何问题
hardsub.exe -v 视频.mkv -s 字幕.ass -a amd -c h265 -q 23 -y

# 3) 指定输出、指定工作目录
hardsub.exe -v "D:\片子\01.mp4" -s zh.ass -o "D:\片子\01_中文硬字幕.mp4"

# 4) 只看参数、不真的压
hardsub.exe -v 视频.mp4 -s 字幕.ass -a cpu -c h264 --dry-run

# 5) HDR 片源转普通 SDR（BT.709 8bit）
hardsub.exe -v HDR视频.mkv -s 字幕.ass --tonemap -y
```

## 参数

| 参数 | 说明 |
| --- | --- |
| `-v, --video` | 输入视频；省略则扫描工作目录并让你选 |
| `-s, --subtitle` | 字幕文件（`.ass/.ssa/.srt/.vtt`）；目录里只有一个时自动使用 |
| `-o, --output` | 输出文件，默认 `<名字>_hardsub_<编码>.mp4/.mkv` |
| `-d, --dir` | 工作目录，默认当前目录 |
| `-a, --accel` | `auto`(默认) / `cpu` / `nvidia` / `amd` / `intel` |
| `-c, --codec` | `h264` / `h265` / `av1`；HDR 源默认自动选 `h265`（Main10） |
| `-q, --quality` | 画质参数 crf/cq/qp，越小越清晰；默认 h264/h265=23，av1=30（SVT-AV1=35） |
| `--preset` | CPU 软编码 preset（`medium` / `slow` / `veryslow`…） |
| `--fontsdir` | 额外字体目录；也会自动识别视频/字幕旁边的 `fonts/` |
| `--tonemap` | HDR → SDR 色调映射，输出 BT.709 8bit（给普通屏幕/播放器看） |
| `-t, --threads` | 限制编码线程数，0=自动 |
| `-y, --yes` | 全部用默认值，不再询问 |
| `--overwrite` | 输出已存在时直接覆盖（默认会拒绝，避免误删） |
| `--dry-run` | 只打印最终 ffmpeg 命令 |
| `--keep-temp` | 保留临时字幕文件（排查问题用） |
| `--verbose` | 结束时打印完整的 ffmpeg 日志（排查压制问题用） |
| `--list` | 列出目录里的视频与字幕 |
| `--probe` | 环境自检 |
| `--no-pause` | 结束时不等待回车（给脚本调用） |

## HDR 视频

**静默自动处理**：程序按 `color_transfer` 识别 `smpte2084`（HDR10 / PQ）和
`arib-std-b67`（HLG）。HDR 源自动走 10bit HDR 链路，普通源走原来的 SDR 链路，
不需要加参数，也不会弹选项问"要不要按 HDR 处理"。

HDR 链路上做的事：

- 滤镜链全程 10bit：CPU 是 `yuv420p10le`，硬件是 `p010le`（旧版写死 `nv12`，
  10bit 源上 `hwdownload` 会直接报 `Invalid output format nv12 for hwframe download`，
  然后整条硬件链路被判失败、回退 CPU）；
- 输出显式写回 `-color_primaries bt2020 -color_trc smpte2084 -colorspace bt2020nc`，
  避免播放器把 PQ 数据当 BT.709 显示（画面发灰）；
- CPU 的 `libx265` 会带上 `hdr-opt=1`（HDR10）、`master-display`、`max-cll`；
  硬件编码器则由帧的 side data 把 Mastering display / MaxCLL 带进码流；
- 压制后自检会核对输出是不是 10bit、色彩标签有没有丢。

### HDR 下的字幕亮度

字幕原本是 SDR 的（BT.709，白 = 100nit）。直接把 SDR 白写进 PQ 视频，255 会被
当成接近峰值的 PQ 信号——实测 10bit 白字约 **9900nit**，在黑场景里白得刺眼。

HDR 链路会把字幕颜色**预映射**到 HDR 的码值，再交给 libass 正常烧录：

| 传递特性 | SDR 白映射到 | 码值（8bit） | 实际亮度 |
| --- | --- | --- | --- |
| HDR10 / PQ | PQ 0.58 信号 | 148/255（`&H949494`） | ≈203nit（BT.2408 图形白） |
| HLG | 75% HLG 信号 | 191/255（`&HBFBFBF`） | 1000nit 屏上 ≈203nit |

处理范围：

- ASS/SSA：样式表里的 `PrimaryColour` / `SecondaryColour` / `OutlineColour` /
  `BackColour`，以及事件里的内联 `\c`、`\1c`~`\4c`（含 `\t(...)` 里的颜色）都会换算；
  透明度、字号、位置、渐变时序不变。
- SRT/VTT：没有样式表，通过 `force_style` 把 libass 默认白/描边改成 HDR 码值。
- 换算在线性光下做 BT.709 → BT.2020，再按 203nit 编码成 PQ 或 HLG；所以不是简单
  把白改成灰色，彩色字幕的色相/亮度关系也保持正常。
- 已知限制：SRT/VTT 内联的 `<font color="#...">` 不会被换算（会按 SDR 色显示得偏亮）；
  需要精确配色的字幕请用 ASS/SSA。位图字幕（PGS/VobSub）本来就不在支持范围内。

要在普通 SDR 屏幕上看，加 `--tonemap`：优先用 `zscale + tonemap(hable)`（纯 CPU），
没有 libzimg 的 ffmpeg 会退到 `libplacebo`。字幕在 tonemap **之后**才烧，
所以字幕是正常的 SDR 白色，不会被一起压暗；这种情况下不做 HDR 配色映射。

几个注意点：

- HDR + `-c h264`：H.264 10bit（High10）兼容性很差，硬件编码器也基本不支持，
  建议用 `-c h265`（程序在 HDR 下不指定编码时已经默认 h265）。
- Dolby Vision 源：重编码后 DV 动态层会丢失，只保留 HDR10 基础层（程序会提示）。

## 进度条为什么不会卡住

ffmpeg 的 `-progress` 走 stdout、日志/警告走 stderr。旧实现让 stderr 继承父进程，
一旦调用方把 stderr 接成管道又没人读，Windows 的 64KB 管道缓冲区写满后
ffmpeg 就卡在写日志上：进程还在、视频不再前进、进度条一动不动。

现在 stdout 和 stderr 各有一个读取线程，警告只会被收集起来、压完以后汇总展示；
进度时间同时认 `out_time_us` / `out_time_ms` / `out_time`（老版本 ffmpeg 没有第一个），
超过 8 秒没有新进度会显示 `[等待 ffmpeg…]`，失败时直接把 ffmpeg 的最后 12 行日志打出来。

## 从源码构建 / 测试

```bash
python hardsub.py --probe                     # 直接跑源码
python tests\test_hardsub.py                  # 63 项单元 + 集成测试（含 HDR 配色 / 进度回归）
python tests\verify_exe.py                    # exe 黑盒验证（需先打包出 exe）

# 打包成单文件 exe
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --workpath _build\work --distpath _build\dist hardsub.spec
```

## 硬件链路对照

解码在 GPU、烧字幕在内存、再传回 GPU 编码：

| 方案 | 输入参数 | 滤镜链（SDR / 8bit） | 滤镜链（HDR / 10bit） |
| --- | --- | --- | --- |
| NVIDIA | `-init_hw_device cuda=cuda:0 -hwaccel cuda` | `format=nv12,hwupload_cuda` | `format=p010le,hwupload_cuda` |
| AMD | `-init_hw_device d3d11va=dx -hwaccel d3d11va` | `format=nv12,hwupload=derive_device=d3d11va` | `format=p010le,hwupload=derive_device=d3d11va` |
| Intel | `-init_hw_device qsv=hw -filter_hw_device hw -hwaccel qsv` | `format=nv12,hwupload=derive_device=qsv` | `format=p010le,hwupload=derive_device=qsv` |
| CPU | 无 | `format=yuv420p` | `format=yuv420p10le` |

> `hwdownload` 后面会先钉死一个下载格式（10bit 源 = `p010le`）：不写死的话，
> 后面接 `zscale` 时滤镜图可能协商出 `gray`，硬件链路直接报
> `Invalid output format gray for hwframe download`。

> 注意：Windows 路径塞进 `-vf` 必须写成 `'D\:/path/x.ass'`，
> 否则盘符的冒号会被当成滤镜选项分隔符（报 `Unable to parse "original_size" …`）。

## License

未指定。
