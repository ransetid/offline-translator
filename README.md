# LiveTranslator · 本地离线双语会议助手

麦克风 + 本机播放的声音（会议软件 / 视频）→ 本地识别（SenseVoice / Whisper）→ 本地翻译（Gemma 等）→ 实时双语字幕 + 自动会议纪要。**全程离线，音频不出本机。**

> 这是 macOS 客户端：模型交给 [oMLX](https://github.com/) 这类本机 OpenAI 兼容运行时。每台 Mac 首次需要：装 oMLX + 加载模型 + 授权屏幕录制。App 内置「环境检查」（左下角状态灯 / 帮助）会逐项检查并给出一键修复。

## v4 有什么新东西

| | |
|---|---|
| **全新界面** | 原生窗口（pywebview / WKWebView）+ 设计系统（深/浅主题，见 [DESIGN.md](DESIGN.md)）。时间线 / 分语种对照两种视图，桌面字幕条可拖动、可调字号。 |
| **会中** | 先出原文、译文逐字流出；★ 标记重点（⌘B），纪要优先体现；随手纠错、重新翻译；电平表上直接看到静音阈值。 |
| **不丢数据** | 结束会议时先收完正在说的半句、等最后几句翻完再收尾；转录逐条落盘、原子写；异常退出的会在记录库一键「恢复」。 |
| **后端不在线也能用** | App 照常打开；可以只录音，事后「从录音重新识别」。 |
| **记录库** | 按日期分组、全文搜索（标题/原文/译文/纪要）、内置播放器（点句子跳播、按说话人自动切音轨、倍速）、纪要模板（通用会议/客户沟通/访谈调研/课程讲座）、说话人命名（跨会议声纹建议）、导出 Markdown / Word / TXT / SRT / VTT。 |
| **设置** | 分组表单、即时校验、连接测试、静音阈值一键「校准」；术语表改成可编辑表格。 |
| **语言** | 中⇄英、中⇄日、中⇄韩、英⇄日。 |

## 安装与运行

```bash
git clone <this-repo> && cd offline-translator
./install.command          # 或双击：建 .venv + 装依赖（几分钟）
./LiveTranslator.command   # 或双击：启动
```

- 需要 Python 3.10+。依赖见 `requirements.txt`。
- 没装 pywebview 时会自动改用浏览器打开界面；也可以 `python -m livetranslator --browser`。
- 旧版 Tk 界面仍保留在 `legacy/translator_v3.py`，共用同一份配置与记录。

### 每台 Mac 一次

1. 打开 **oMLX.app**，加载识别模型 `SenseVoiceSmall`（快）或 `whisper-large-v3-turbo`（准），以及一个对话模型（如 `gemma-4-e2b-it-4bit`）。
2. 首次运行按向导授权 **屏幕录制**（抓系统 / 会议声音）。授权后点控制台里的「重试」；个别系统需重启 App。
3. 麦克风权限在第一次说话时系统会询问。

## 快捷键

| | |
|---|---|
| ⌘R | 开始 / 结束会议 |
| ⌘B | 标记最近一句 |
| ⌘⇧S | 字幕条 |
| ⌘1–4 | 实时 / 记录库 / 术语表 / 设置 |
| ⌘F | 搜索记录 |
| 空格 | 记录库里播放 / 暂停 |

## 数据存哪 / 怎么删

- 默认 `~/Library/Application Support/LiveTranslator/`：`config.json`、`terms.json`、`speakers.json`、`records/<id>/`（`audio/*.wav`、`transcript.jsonl`、`summary.md`、`meta.json`）。
- 保存位置可在 **设置 → 存储** 修改；记录库「打开文件夹」直接在访达里定位。
- 卸载 = 删上面的文件夹 + 删 App。

## 架构

```
livetranslator/
  audio.py     音源（sounddevice 麦克风 / ScreenCaptureKit 系统声音，回退 BlackHole）
  pipeline.py  VAD 分句、语种检测、幻听过滤、说话人聚类、翻译 prompt（纯逻辑，可单测）
  backend.py   OpenAI 兼容 HTTP 客户端（流式、友好错误、自托管 mlx_lm 兜底）
  engine.py    运行时：会话生命周期、工作线程、后台任务、事件总线
  records.py   记录读写（原子写）、导出、声纹库
  server.py    127.0.0.1 HTTP：UI + JSON API + SSE + 带 Range 的音频（token + Host 校验）
  app.py       pywebview 外壳：主窗口 + 字幕条；关窗时自动收尾
  ui/          tokens.css · app.css · app.js · overlay.html（无框架、无外网依赖）
```

## 测试

```bash
python3 tests/test_e2e.py          # 假后端 + 合成音频：会话 → 落盘 → 纪要 → 记录库 → HTTP 层
python3 tests/run_demo.py 8765     # 界面演示（假后端 + 实时喂合成音频），浏览器打开即可
```

打包成 `.app` 见 [DISTRIBUTION.md](DISTRIBUTION.md)。
