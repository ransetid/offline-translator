"""LiveTranslator —— 本地离线双语会议助手。

包结构：
  config    配置 / 术语表 / 语言常量
  audio     音源（麦克风 / ScreenCaptureKit 系统声音）+ 重采样
  backend   OpenAI 兼容后端（oMLX / mlx_lm.server）HTTP 客户端
  pipeline  分句(VAD) → 识别 → 过滤 → 说话人 → 翻译 的纯逻辑
  summary   自动标题 + 纪要模板（map-reduce）
  records   一场会 = 一个目录；记录库查询、导出、声纹
  engine    UI 无关的运行时：会话生命周期、后台任务、事件总线
  server    本机 HTTP：静态 UI + JSON API + SSE 事件 + 带 Range 的音频
  app       pywebview 外壳（主窗口 + 字幕条）；无 pywebview 时退化为浏览器
"""

__version__ = "4.0.0"
