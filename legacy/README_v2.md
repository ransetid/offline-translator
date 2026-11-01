# 实时翻译 v2（双音源 + oMLX 版）— 安装与使用

架构：麦克风 + BlackHole 双路采集 → oMLX 识别（Whisper）→ oMLX 翻译（Gemma 4B）→ 字幕 / 会议记录 / 总结。全程离线。

## 一、准备 oMLX（你已装，确认两件事）

```bash
# 1. 确认版本 >= 0.3.0 且带音频支持（语音识别在 0.3.0 加入）
omlx --version
pip install 'omlx[audio]'   # 如果识别接口报 404，补装这个

# 2. 启动后查看已加载的模型
curl http://127.0.0.1:16578/v1/models

```

在 oMLX 管理面板里确保有两个模型：
- **翻译**：你已有的 Gemma 4B（记下它在列表里的确切名字）
- **识别**：下载一个 Whisper 模型，推荐 `whisper-large-v3-turbo`（速度快、中英日都准）；嫌大就 `whisper-small`

把 `translator_v2.py` 顶部的 `CHAT_MODEL`、`ASR_MODEL` 改成 `/v1/models` 列出的实际名称。

## 二、安装 BlackHole（采集会议软件声音）

```bash
brew install blackhole-2ch
```

装完后配置音频路由（关键步骤，只需配一次）：

1. 打开 **音频 MIDI 设置**（聚焦搜索 Audio MIDI Setup）
2. 左下角 ➕ → **创建多输出设备**，勾选 **你的扬声器/耳机 + BlackHole 2ch**
3. 系统设置 → 声音 → **输出** 选这个"多输出设备"

这样会议软件的声音会同时进你的耳朵和 BlackHole，程序从 BlackHole 读到的就是"远端"音频。

> 不开会时记得把输出切回普通扬声器，多输出设备下系统音量键不可用。

## 三、安装依赖并运行

```bash
python3 -m venv venv && source venv/bin/activate
pip install sounddevice numpy requests
# 若提示缺 PortAudio: brew install portaudio

python translator_v2.py
```

启动后会显示检测到的音源。说话/播放会议声音，停顿约 1 秒出字幕：

```
[14:32:05] [远端] (英文)
  原文: Let's move on to the budget discussion.
  译文: 我们继续讨论预算问题。   (1.8s)

[14:32:18] [本地] (中文)
  原文: 好的，我先说一下目前的开支情况。
  译文: Sure, let me first go over our current expenses.   (2.1s)
```

**Ctrl+C 结束** → 转录和总结自动存到 `~/live_translator_records/`。

## 四、配置说明

| 需求 | 改配置区哪里 |
|---|---|
| 切换中日互译 | `TRANSLATE_MODE = "zh-ja"` |
| 翻译质量不满意想换 Qwen | oMLX 里下载 Qwen 的 MLX 版本，改 `CHAT_MODEL` |
| 麦克风环境吵误触发 | `SILENCE_THRESHOLD_MIC` 调到 0.02~0.03 |
| 句子被切碎 | `SILENCE_BLOCKS_TO_CUT` 调到 3 |

## 五、测试建议

1. 先单测麦克风：不开会议软件，直接说中文和英文各几句
2. 再单测远端：YouTube 放一段英文演讲，确认 [远端] 字幕正常
3. 最后实战：开一场真实线上会议（半线下场景：你的发言和现场同事走麦克风=[本地]，线上参会人走 BlackHole=[远端]）

重点观察延迟和准确率。如果 Gemma 4B 翻译质量不行（尤其注意中→日方向），告诉我实际的坏例子，我们决定换模型还是调提示词。

## 六、已知限制

- [本地]/[远端] 是按音源区分，远端多人发言暂不细分（第二阶段加声纹识别）
- 你自己的声音如果外放，可能被麦克风拾到造成重复字幕——开会用耳机即可避免
- 识别和翻译串行处理，连续快速对话时字幕会排队，延迟逐渐累积；测试时留意这点，是后续优化重点
