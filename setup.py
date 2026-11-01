"""
py2app 打包脚本（把本工具打成 macOS .app）。
⚠️ 这是**起点**，不是一次成功——PyObjC / sounddevice(PortAudio) / av(ffmpeg) / sherpa-onnx
都带原生 dylib，py2app 常需要几轮 build→报错→补 includes/frameworks→再 build。见 DISTRIBUTION.md。

用法：
    .venv/bin/pip install py2app
    .venv/bin/python setup.py py2app        # 产物在 dist/LiveTranslator.app
    # 若缺库/崩溃，按报错往 OPTIONS 的 packages/includes/frameworks 里补，再重跑

注意：
- 本 app 仍需 **oMLX 在运行**（模型运行时）；.app 只打包客户端，不含大模型。
- self_host（mlx_lm.server 子进程）在 .app 里通常起不来（sys.executable 变成 app 二进制），
  分发时建议依赖 oMLX，别指望 self-host。
"""
from setuptools import setup

APP = ["main.py"]
DATA_FILES = ["DESIGN.md"]

OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "LiveTranslator",
        "CFBundleDisplayName": "本地实时翻译",
        "CFBundleIdentifier": "com.livetranslator.app",
        "CFBundleShortVersionString": "4.0.0",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        # 麦克风要用途说明（否则申请权限会崩）；屏幕录制无 plist key，运行时 TCC 授权
        "NSMicrophoneUsageDescription": "采集麦克风语音做实时翻译与转录（音频不出本机）。",
    },
    # 这些带原生扩展/依赖，py2app 未必自动跟到，先显式列出，按 build 报错继续补
    "packages": [
        "objc", "Foundation", "AppKit", "Quartz", "ScreenCaptureKit", "CoreMedia",
        "numpy", "scipy", "sounddevice", "requests", "urllib3", "certifi",
        "charset_normalizer", "idna", "sherpa_onnx", "av", "huggingface_hub",
        "webview", "livetranslator", "WebKit",
    ],
    "includes": ["queue", "json", "wave"],
    "excludes": ["tkinter.test", "test", "pytest"],
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
