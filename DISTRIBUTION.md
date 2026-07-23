# 分发说明

这是一个**客户端 app**：它自己不跑大模型，模型交给 **oMLX.app**（本机 OpenAI 兼容的模型运行时）。
所以给任何人用，前提都是：装好 oMLX + 在 oMLX 里加载模型 + 授权屏幕录制。app 内置的**使用向导**
（首次自动弹，或点「帮助」）会带着一步步检查并修复这些。

## 前提（每台 Mac 一次）
1. 装并打开 **oMLX.app**，在里面加载：
   - 识别模型：`SenseVoiceSmall`（快）或 `whisper-large-v3-turbo`（更准）
   - 翻译/摘要模型：一个对话模型（如 `gemma-4-e2b-it-4bit`）
2. 首次运行按向导**授权「屏幕录制」**（抓系统/会议声音用），授权后重启 app。
3. 麦克风权限首次说话时系统会问，点允许。

## 分发方式一：双击运行（推荐，最可靠）
适合团队内部。把整个项目文件夹给对方：
- 对方双击 **`install.command`** 装依赖（建 .venv + pip）。
- 之后双击 **`LiveTranslator.command`** 启动。

优点：不折腾原生库打包，稳。缺点：是「双击脚本」不是图标式 .app，且带一个 .venv 文件夹。

## 分发方式二：打成 .app（py2app，需调试）
真正的图标式 `.app`：
```bash
.venv/bin/pip install py2app
.venv/bin/python setup.py py2app      # 产物 dist/LiveTranslator.app
```
**这一步几乎不会一次成功**——本项目带一堆原生依赖，py2app 要几轮迭代：

常见坑与对策：
- **PyObjC 框架**（ScreenCaptureKit/Quartz/AppKit）：漏了就往 `setup.py` 的 `packages` 补。
- **sounddevice**：依赖 PortAudio dylib。若 .app 里报找不到 `libportaudio`，用 `--frameworks` 把
  `.venv/.../_sounddevice_data/portaudio-binaries/libportaudio.dylib` 带进去。
- **av (PyAV)**：捆着 ffmpeg 的 dylib，体积大、易漏。仅「导入文件」用到；实在搞不定可先把导入功能
  做成可选、打包时不带 av。
- **sherpa-onnx**：带 onnxruntime dylib，同理按报错补 frameworks。
- **Tcl/Tk**：py2app 一般会自动带；界面空白多半是 Tk 没打进去。
- **self-host LLM 起不来**：.app 里 `sys.executable` 是 app 二进制，`python -m mlx_lm.server` 跑不了。
  分发场景**依赖 oMLX**，不要指望 self-host（`self_host_llm` 影响不大，因为优先用外部 oMLX）。
- 首启崩溃：`dist/LiveTranslator.app/Contents/MacOS/LiveTranslator` 在终端里直接跑，看报错最快。

签名/公证（给公司外的人才需要）：`codesign` + `notarytool`，内部分发可先跳过（对方右键→打开）。

## 数据与卸载
- 所有数据在 `~/Library/Application Support/LiveTranslator/`（配置、术语表、声纹、records/ 录音与纪要）。
- 卸载 = 删这个文件夹 + 删 app。记录不在用户可见目录堆，走 app 内「记录库」管理、「导出」取文件。
