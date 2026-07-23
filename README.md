# LiveTranslator · 本地离线双语会议助手

麦克风 + 媒体声音双路采集 → 本地识别（SenseVoice / Whisper）→ 本地翻译（Gemma 等）→ 一屏两半的双语字幕 + 会议记录 + 自动纪要。**全程离线，音频不出本机。**

> 这是一个 **macOS 客户端**：它自己不跑大模型，模型交给 [oMLX](https://github.com/) 这类本机 OpenAI 兼容运行时。因此每台 Mac 用前都要：装 oMLX + 加载模型 + 授权屏幕录制。app 内置「使用向导」会带你逐项检查。

## 前提（每台 Mac 一次）

1. 装并打开 **oMLX.app**，在里面加载：
   - 识别模型：`SenseVoiceSmall`（快）或 `whisper-large-v3-turbo`（更准）
   - 翻译/纪要模型：一个对话模型（如 `gemma-4-e2b-it-4bit`）
2. 首次运行按向导**授权「屏幕录制」**（抓系统/会议声音用），授权后重启 app。
3. 麦克风权限首次说话时系统会问，点允许。

## 安装与运行（推荐：源码 + 脚本）

```bash
git clone <this-repo>
cd offline-translator
./install.command        # 或双击：建 .venv + 装依赖（几分钟）
./LiveTranslator.command # 或双击：启动
```

> 需要 Python 3.10+。依赖含 PyObjC / sounddevice(PortAudio) / sherpa-onnx / PyAV 等原生库。

## 数据存哪 / 怎么删

- 所有数据默认在 `~/Library/Application Support/LiveTranslator/`（配置、术语表、声纹、`records/` 录音与纪要）。
- 保存目录可在 **设置 → 记录保存目录** 里改，旁边「打开」按钮直接在访达里定位该文件夹，方便查看和删除。
- app 内「记录库」可逐条查看 / 重命名 / 删除 / 导出。
- 卸载 = 删上面这个文件夹 + 删 app。

## 打成 .app（可选，需调试）

真正的图标式 `.app` 走 py2app，但带一堆原生 dylib，通常要几轮迭代，且分发给其他 Mac 还需签名/公证。见 [DISTRIBUTION.md](DISTRIBUTION.md)。

## 设计

视觉设计系统见 [DESIGN.md](DESIGN.md)：夜里的广播字幕间——暗色、单一强调色、克制。
