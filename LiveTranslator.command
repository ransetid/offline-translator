#!/bin/bash
# 双击即可运行（从源码启动）。首次请先双击 install.command 安装依赖。
cd "$(dirname "$0")" || exit 1
if [ ! -x ".venv/bin/python" ]; then
  echo "未找到 .venv，请先双击运行 install.command 安装依赖。"
  read -r -p "按回车关闭…" _
  exit 1
fi
# 新界面（pywebview 原生窗口）；没装 pywebview 时会自动改用浏览器打开
exec .venv/bin/python -m livetranslator "$@"
