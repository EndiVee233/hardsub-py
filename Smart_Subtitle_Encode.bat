@echo off
:: 强制补全系统核心路径，保证系统命令可用
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%PATH%"
:: 强制切换到脚本所在目录
cd /d "%~dp0"

chcp 936 >nul
setlocal enabledelayedexpansion
title 智能硬编码字幕 v3.11（NVENC参数精简版）

:: 自动清理残留文件
if exist "_temp_sub.ass" del "_temp_sub.ass" 2>nul

:: ---------- 检测FFmpeg（直接用当前目录下的 exe） ----------
if not exist "ffmpeg.exe" (
    where ffmpeg >nul 2>nul
    if !errorlevel! neq 0 (
        echo [严重错误] 未检测到 FFmpeg。请将 ffmpeg.exe 放在本脚本同一目录！
        pause
        exit /b
    )
)
if not exist "ffprobe.exe" (
    where ffprobe >nul 2>nul
    if !errorlevel! neq 0 (
        echo [错误] 未检测到 ffprobe，请确认 FFmpeg 安装包包含了该组件！
        pause
        exit /b
    )
)
echo [系统] FFmpeg 环境检测正常。

echo.
echo ================================================
echo   [提示] 为确保脚本稳定，请确保当前文件夹路径
echo         以及所有文件名中【不包含】感叹号(!)字符！
echo ================================================
echo.

:: ---------- 1. 检测CPU核心数 ----------
for /f "tokens=2 delims==" %%a in ('wmic cpu get NumberOfCores /value ^| find "="') do set "CPU_CORES=%%a"
if not defined CPU_CORES set "CPU_CORES=14"
echo [系统] CPU物理核心数：%CPU_CORES%

:: ---------- 2. 检测显卡 ----------
echo [系统] 正在检测显卡驱动...
set "gpu_info="
for /f "delims=" %%a in ('powershell -NoProfile -Command "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name" 2^>nul') do (
    if not defined gpu_info (
        set "gpu_info=%%a"
    ) else (
        set "gpu_info=!gpu_info! / %%a"
    )
)
echo [系统] 检测到显卡：!gpu_info!

:: ---------- 3. 检测厂商支持 ----------
set "SUPPORT_NVIDIA=0"
set "SUPPORT_AMD=0"
set "SUPPORT_INTEL=0"
echo !gpu_info! | findstr /i "NVIDIA" >nul && set "SUPPORT_NVIDIA=1"
echo !gpu_info! | findstr /i "AMD" >nul && set "SUPPORT_AMD=1"
echo !gpu_info! | findstr /i "Intel" >nul && set "SUPPORT_INTEL=1"

:: ---------- 4. 选择加速方案 ----------
echo.
echo ====== 请选择硬件加速方案 ======
set "gpu_has_hw=0"

if "!SUPPORT_NVIDIA!"=="1" (
    echo   [1] NVIDIA 硬件加速 （NVENC）
    set "gpu_has_hw=1"
)
if "!SUPPORT_AMD!"=="1" (
    echo   [2] AMD 硬件加速 （AMF）
    set "gpu_has_hw=1"
)
if "!SUPPORT_INTEL!"=="1" (
    echo   [3] Intel 硬件加速 （QSV）
    set "gpu_has_hw=1"
)
echo   [4] 纯 CPU 软编码（万能兼容）

if "!gpu_has_hw!"=="0" (
    echo [警告] 未检测到可用硬件加速，将强制使用 CPU 软编码。
    set "gpu_vendor=CPU"
) else (
    :gpu_loop
    set "gpu_choice="
    set /p "gpu_choice=请输入编号 （1/2/3/4）：" 
    if "!gpu_choice!"=="" (
        echo [错误] 不能为空，请重新输入！
        goto :gpu_loop
    )
    echo !gpu_choice!| findstr /r "^[0-9]*$" >nul || (
        echo [错误] 必须输入纯数字！
        goto :gpu_loop
    )
    
    if "!gpu_choice!"=="1" (
        if "!SUPPORT_NVIDIA!"=="1" ( set "gpu_vendor=NVIDIA" & goto :gpu_done ) else ( echo [错误] 该显卡不支持或未被检测到，请重新输入！ & goto :gpu_loop )
    )
    if "!gpu_choice!"=="2" (
        if "!SUPPORT_AMD!"=="1" ( set "gpu_vendor=AMD" & goto :gpu_done ) else ( echo [错误] 该显卡不支持或未被检测到，请重新输入！ & goto :gpu_loop )
    )
    if "!gpu_choice!"=="3" (
        if "!SUPPORT_INTEL!"=="1" ( set "gpu_vendor=INTEL" & goto :gpu_done ) else ( echo [错误] 该显卡不支持或未被检测到，请重新输入！ & goto :gpu_loop )
    )
    if "!gpu_choice!"=="4" (
        set "gpu_vendor=CPU" & goto :gpu_done
    ) else (
        echo [错误] 无效选择，请重新输入！
        goto :gpu_loop
    )
    
    :gpu_done
    echo [确认] 当前加速方案：!gpu_vendor!
)

:: 配置硬件参数
if "!gpu_vendor!"=="CPU" (
    set "input_opt=-hwaccel auto"
    set "hwupload_filter=hwupload"
    set "enc_suffix=x264"
) else if "!gpu_vendor!"=="NVIDIA" (
    set "input_opt=-init_hw_device cuda=cuda:0 -hwaccel cuda -hwaccel_output_format cuda"
    set "hwupload_filter=hwupload_cuda"
    set "enc_suffix=nvenc"
) else if "!gpu_vendor!"=="AMD" (
    set "input_opt=-init_hw_device d3d11va=d3d11va:0 -hwaccel d3d11va -hwaccel_output_format d3d11"
    set "hwupload_filter=hwupload=d3d11va"
    set "enc_suffix=amf"
) else if "!gpu_vendor!"=="INTEL" (
    set "input_opt=-init_hw_device qsv=qsv:hw -hwaccel qsv -hwaccel_output_format qsv"
    set "hwupload_filter=hwupload=qsv"
    set "enc_suffix=qsv"
)

:: ---------- 5. 扫描视频文件 ----------
set "ext_list=.mp4 .flv .webm .mov .avi .mkv .m4v .ts"
set "video_count=0"
echo.
echo [扫描] 当前目录: %cd%
echo [扫描] 正在扫描所有文件...
for /f "delims=" %%f in ('dir /b /a-d') do (
    set "ext=%%~xf"
    set "is_match=0"
    
    for %%e in (%ext_list%) do (
        if /i "!ext!"=="%%e" set "is_match=1"
    )
    
    if "!is_match!"=="1" (
        echo [扫描] 找到视频: "%%f"
        set /a video_count+=1
        set "video_!video_count!=%%f"
        set "video_name_!video_count!=%%~nf"
        set "video_ext_!video_count!=%%~xf"
    ) else (
        echo [扫描] 非视频文件，跳过: "%%f"
    )
)
echo [扫描] 扫描完成，共找到 %video_count% 个视频文件。
if %video_count% equ 0 (
    echo [错误] 当前目录未找到任何视频文件
    pause
    exit /b
)

:: ---------- 6. 选择视频与字幕 ----------
echo 找到 %video_count% 个视频文件：
for /l %%i in (1,1,%video_count%) do echo   %%i. !video_%%i!

:ask_video
set "video_choice="
set /p "video_choice=请选择视频编号 （1-%video_count%）：" 
if "!video_choice!"=="" goto :ask_video
echo !video_choice!| findstr /r "^[0-9]*$" >nul || ( echo [错误] 必须输入纯数字！ & goto :ask_video )
if !video_choice! lss 1 goto :ask_video
if !video_choice! gtr %video_count% goto :ask_video
set "input_video=!video_%video_choice%!"
set "input_name=!video_name_%video_choice%!"
set "input_ext=!video_ext_%video_choice%!"

echo.
echo [扫描] 正在扫描 .ass 字幕文件...
set "ass_count=0"
for %%f in (*.ass) do (
    echo [扫描] 检查文件: "%%f"
    set /a ass_count+=1
    set "ass_!ass_count!=%%f"
)
echo [扫描] 扫描完成，共找到 %ass_count% 个字幕文件。
if %ass_count% equ 0 ( echo [错误] 未找到字幕文件 & pause & exit /b )
echo.
echo 找到 %ass_count% 个字幕文件：
for /l %%i in (1,1,%ass_count%) do echo   %%i. !ass_%%i!

:ask_ass
set "ass_choice="
set /p "ass_choice=请选择字幕编号 （1-%ass_count%）：" 
if "!ass_choice!"=="" goto :ask_ass
echo !ass_choice!| findstr /r "^[0-9]*$" >nul || ( echo [错误] 必须输入纯数字！ & goto :ask_ass )
if !ass_choice! lss 1 goto :ask_ass
if !ass_choice! gtr %ass_count% goto :ask_ass
set "subtitle_file=!ass_%ass_choice%!"

:: 创建临时字幕
set "temp_sub=_temp_sub.ass"
copy "!subtitle_file!" "!temp_sub!" >nul
if not exist "!temp_sub!" ( echo [错误] 临时字幕创建失败，请检查权限 & pause & exit /b )

:: ---------- 7. 选择编码格式 ----------
echo.
echo ====== 请选择视频编码格式 ======
set "SUPPORT_H264=0"
set "SUPPORT_HEVC=0"
set "SUPPORT_AV1=0"

if "!gpu_vendor!"=="CPU" (
    set "SUPPORT_H264=1"
    set "SUPPORT_HEVC=1"
    set "SUPPORT_AV1=1"
    echo   [1] H.264 （AVC）  [CPU软编码 - libx264]
    echo   [2] H.265 （HEVC） [CPU软编码 - libx265]
    echo   [3] AV1          [CPU软编码 - libsvtav1]
) else (
    for /f "tokens=*" %%a in ('ffmpeg.exe -hide_banner -encoders 2^>nul ^| findstr /i "!enc_suffix!"') do (
        echo %%a | findstr /i "h264_!enc_suffix!" >nul && set "SUPPORT_H264=1"
        echo %%a | findstr /i "hevc_!enc_suffix!" >nul && set "SUPPORT_HEVC=1"
        echo %%a | findstr /i "av1_!enc_suffix!" >nul && set "SUPPORT_AV1=1"
    )
    if "!gpu_vendor!"=="INTEL" (
        echo !gpu_info! | findstr /i "Arc" >nul
        if !errorlevel! neq 0 set "SUPPORT_AV1=0"
    )
    if "!gpu_vendor!"=="AMD" (
        echo !gpu_info! | findstr /i "RX 7000" >nul
        if !errorlevel! neq 0 set "SUPPORT_AV1=0"
    )
    if "!SUPPORT_H264!"=="1" ( echo   [1] H.264 （AVC）  [!gpu_vendor! 硬件加速] ) else ( echo   [1] H.264 （AVC）  [仅支持CPU软编码] )
    if "!SUPPORT_HEVC!"=="1" ( echo   [2] H.265 （HEVC） [!gpu_vendor! 硬件加速] ) else ( echo   [2] H.265 （HEVC） [仅支持CPU软编码] )
    if "!SUPPORT_AV1!"=="1" ( echo   [3] AV1          [!gpu_vendor! 硬件加速] ) else ( echo   [3] AV1          [仅支持CPU软编码] )
)

:ask_codec
set "codec_choice="
set "codec_name="
set /p "codec_choice=请输入编号 （1/2/3）：" 
if "!codec_choice!"=="" goto :ask_codec
echo !codec_choice!| findstr /r "^[0-9]*$" >nul || ( echo [错误] 必须输入纯数字！ & goto :ask_codec )
if "!codec_choice!"=="1" set "codec_name=H.264"
if "!codec_choice!"=="2" set "codec_name=H.265 （HEVC）"
if "!codec_choice!"=="3" set "codec_name=AV1"
if not defined codec_name goto :ask_codec

:: 自动回退
set "force_cpu=0"
if "!gpu_vendor!"=="NVIDIA" (
    if "!codec_choice!"=="1" if "!SUPPORT_H264!"=="0" set "force_cpu=1"
    if "!codec_choice!"=="2" if "!SUPPORT_HEVC!"=="0" set "force_cpu=1"
    if "!codec_choice!"=="3" if "!SUPPORT_AV1!"=="0" set "force_cpu=1"
)
if "!gpu_vendor!"=="AMD" (
    if "!codec_choice!"=="1" if "!SUPPORT_H264!"=="0" set "force_cpu=1"
    if "!codec_choice!"=="2" if "!SUPPORT_HEVC!"=="0" set "force_cpu=1"
    if "!codec_choice!"=="3" if "!SUPPORT_AV1!"=="0" set "force_cpu=1"
)
if "!gpu_vendor!"=="INTEL" (
    if "!codec_choice!"=="1" if "!SUPPORT_H264!"=="0" set "force_cpu=1"
    if "!codec_choice!"=="2" if "!SUPPORT_HEVC!"=="0" set "force_cpu=1"
    if "!codec_choice!"=="3" if "!SUPPORT_AV1!"=="0" set "force_cpu=1"
)
if "!force_cpu!"=="1" (
    echo [警告] 当前显卡不支持该格式的硬件编码，已自动回退至 CPU 软编码。
    set "gpu_vendor=CPU"
    set "input_opt=-hwaccel auto"
)

:: ★★★ 核心修改：去掉了NVIDIA的spatial_aq和temporal_aq参数 ★★★
if "!gpu_vendor!"=="CPU" (
    if "!codec_choice!"=="1" set "encoder=libx264"
    if "!codec_choice!"=="2" set "encoder=libx265"
    if "!codec_choice!"=="3" set "encoder=libsvtav1"
    set "encoder_params=-crf 23 -preset medium"
) else if "!gpu_vendor!"=="NVIDIA" (
    if "!codec_choice!"=="1" set "encoder=h264_nvenc"
    if "!codec_choice!"=="2" set "encoder=hevc_nvenc"
    if "!codec_choice!"=="3" set "encoder=av1_nvenc"
    set "encoder_params=-rc vbr -cq 23 -b:v 0 -preset p6"
) else if "!gpu_vendor!"=="AMD" (
    if "!codec_choice!"=="1" set "encoder=h264_amf"
    if "!codec_choice!"=="2" set "encoder=hevc_amf"
    if "!codec_choice!"=="3" set "encoder=av1_amf"
    set "encoder_params=-rc cqp -qp_i 23 -qp_p 23 -quality quality"
) else if "!gpu_vendor!"=="INTEL" (
    if "!codec_choice!"=="1" set "encoder=h264_qsv"
    if "!codec_choice!"=="2" set "encoder=hevc_qsv"
    if "!codec_choice!"=="3" set "encoder=av1_qsv"
    set "encoder_params=-global_quality 23 -look_ahead 1"
)

echo 当前 %codec_name% 默认 cq/crf/qp = 23
set "custom_cq="
set /p "custom_cq=是否修改？直接回车默认，或输入新数值："
if not "!custom_cq!"=="" (
    if "!gpu_vendor!"=="NVIDIA" set "encoder_params=-rc vbr -cq !custom_cq! -b:v 0 -preset p6"
    if "!gpu_vendor!"=="AMD" set "encoder_params=-rc cqp -qp_i !custom_cq! -qp_p !custom_cq! -quality quality"
    if "!gpu_vendor!"=="INTEL" set "encoder_params=-global_quality !custom_cq! -look_ahead 1"
    if "!gpu_vendor!"=="CPU" set "encoder_params=-crf !custom_cq! -preset medium"
)

:: ---------- 8. 构建命令并执行 ----------
set "out_ext=%input_ext%"
if /i "%input_ext%"==".webm" set "out_ext=.mkv"
set "output_file=%input_name%_hardsub_%codec_name%%out_ext%"
set "faststart_flag="
if /i "%out_ext%"==".mp4" set "faststart_flag=-movflags +faststart"
if /i "%out_ext%"==".mov" set "faststart_flag=-movflags +faststart"

set "gop_value=120"
for /f "tokens=*" %%a in ('ffprobe.exe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of csv^=p^=0 "!input_video!" 2^>nul') do set "fps_ratio=%%a"
if defined fps_ratio (
    for /f "tokens=1,2 delims=/" %%b in ("!fps_ratio!") do (
        set /a "fps_num=%%b, fps_den=%%c"
        if !fps_den! neq 0 (
            set /a "fps_approx=(!fps_num!*1000)/!fps_den!"
            set /a "gop_value=(!fps_approx!*2)/1000"
            if !gop_value! lss 1 set "gop_value=1"
        )
    )
)

if "!gpu_vendor!"=="CPU" (
    set "filter_opt=hwdownload,subtitles=!temp_sub!:threads=%CPU_CORES%,format=yuv420p"
) else (
    set "filter_opt=hwdownload,subtitles=!temp_sub!:threads=%CPU_CORES%,format=nv12,!hwupload_filter!"
)

echo.
echo ================================================
echo  加速方案：!gpu_vendor!
echo  编码格式：!codec_name! (!encoder!)
echo  输入参数：!input_opt!
echo  滤镜链：!filter_opt!
echo ================================================
echo.
choice /C YN /M "确认开始处理？"
if errorlevel 2 (
    del "!temp_sub!" 2>nul
    echo 已取消。
    pause
    exit /b
)

echo 正在处理，请耐心等待...
ffmpeg.exe -y !input_opt! -i "!input_video!" -vf "!filter_opt!" -c:v !encoder! !encoder_params! -g !gop_value! !faststart_flag! -acodec copy "!output_file!" -stats

if !errorlevel! equ 0 (
    del "!temp_sub!" 2>nul
    echo.
    echo  处理成功！输出文件：!output_file!
) else (
    del "!temp_sub!" 2>nul
    echo.
    echo  处理失败！
    echo [提示] 如果选择了硬件加速但失败，可能是驱动过旧或不支持该编码。
    echo [建议] 请尝试重新运行脚本，在“选择加速方案”时选择 [4] 纯CPU软编码。
)
echo.
pause