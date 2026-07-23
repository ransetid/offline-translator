#!/bin/bash
# 给同事：双击这个先装依赖（建 venv + pip 装包）。装完双击 LiveTranslator.command 运行。
cd "$(dirname "$0")" || exit 1
echo "=== 创建虚拟环境 + 安装依赖（几分钟）==="
python3 -m venv .venv || { echo "需要 Python 3.10+"; read -r -p "回车关闭…" _; exit 1; }
.venv/bin/pip install --upgrade pip
.venv/bin/pip install numpy scipy requests sounddevice \
  pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz \
  pyobjc-framework-ScreenCaptureKit pyobjc-framework-CoreMedia pyobjc-framework-libdispatch \
  sherpa-onnx av python-docx huggingface_hub mlx-lm
echo
echo "=== 依赖装好了。还需要： ==="
echo "1) 装并打开 oMLX.app，在里面加载模型：SenseVoiceSmall（识别）+ 一个对话模型（翻译，如 gemma）。"
echo "2) 双击 LiveTranslator.command 启动；首次会有使用向导带你过一遍。"
read -r -p "按回车关闭…" _
