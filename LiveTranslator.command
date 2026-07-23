#!/bin/bash
# 双击即可运行（从源码启动，最省事、最可靠的分发方式）。
# 首次给同事：连同整个文件夹（含 .venv）一起拷，或让对方先跑 install.command。
cd "$(dirname "$0")" || exit 1
if [ ! -x ".venv/bin/python" ]; then
  echo "未找到 .venv，请先双击运行 install.command 安装依赖。"
  read -r -p "按回车关闭…" _
  exit 1
fi
exec .venv/bin/python translator_v3.py
