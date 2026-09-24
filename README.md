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
| 没有进度、压完不知道对不对 | 进度条 + 压制后自检（时长 / 分辨率 / 音轨数） |
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
```

## 参数

| 参数 | 说明 |
| --- | --- |
| `-v, --video` | 输入视频；省略则扫描工作目录并让你选 |
| `-s, --subtitle` | 字幕文件（`.ass/.ssa/.srt/.vtt`）；目录里只有一个时自动使用 |
| `-o, --output` | 输出文件，默认 `<名字>_hardsub_<编码>.mp4/.mkv` |
| `-d, --dir` | 工作目录，默认当前目录 |
| `-a, --accel` | `auto`(默认) / `cpu` / `nvidia` / `amd` / `intel` |
| `-c, --codec` | `h264`(默认) / `h265` / `av1` |
| `-q, --quality` | 画质参数 crf/cq/qp，越小越清晰；默认 h264/h265=23，av1=30（SVT-AV1=35） |
| `--preset` | CPU 软编码 preset（`medium` / `slow` / `veryslow`…） |
| `--fontsdir` | 额外字体目录；也会自动识别视频/字幕旁边的 `fonts/` |
| `-t, --threads` | 限制编码线程数，0=自动 |
| `-y, --yes` | 全部用默认值，不再询问 |
| `--overwrite` | 输出已存在时直接覆盖（默认会拒绝，避免误删） |
| `--dry-run` | 只打印最终 ffmpeg 命令 |
| `--keep-temp` | 保留临时字幕文件（排查问题用） |
| `--list` | 列出目录里的视频与字幕 |
| `--probe` | 环境自检 |
| `--no-pause` | 结束时不等待回车（给脚本调用） |

## 从源码构建 / 测试

```bash
python hardsub.py --probe                     # 直接跑源码
python tests\test_hardsub.py                  # 35 项单元 + 集成测试
python tests\verify_exe.py                    # exe 黑盒验证（需先打包出 exe）

# 打包成单文件 exe
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --workpath _build\work --distpath _build\dist hardsub.spec
```

## 硬件链路对照

解码在 GPU、烧字幕在内存、再传回 GPU 编码：

| 方案 | 输入参数 | 滤镜链尾部 |
| --- | --- | --- |
| NVIDIA | `-init_hw_device cuda=cuda:0 -hwaccel cuda` | `format=nv12,hwupload_cuda` |
| AMD | `-init_hw_device d3d11va=dx -hwaccel d3d11va` | `format=nv12,hwupload=derive_device=d3d11va` |
| Intel | `-init_hw_device qsv=hw -filter_hw_device hw -hwaccel qsv` | `format=nv12,hwupload=derive_device=qsv` |
| CPU | 无 | `format=yuv420p` |

> 注意：Windows 路径塞进 `-vf` 必须写成 `'D\:/path/x.ass'`，
> 否则盘符的冒号会被当成滤镜选项分隔符（报 `Unable to parse "original_size" …`）。

## License

未指定。
