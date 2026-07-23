---
version: alpha
name: LiveTranslator
description: 本地离线双语会议助手的视觉设计系统（暗色、专注、可信）
colors:
  bg: "#14151c"
  surface: "#1b1d26"
  panel: "#10121a"
  hairline: "#2b2e3c"
  text: "#e7e9f2"
  text-dim: "#a6abbd"
  text-muted: "#6b7085"
  accent: "#6fd6a8"
  accent-press: "#59c295"
  lang-zh: "#93e6b0"
  lang-en: "#8fb8ff"
  lang-ja: "#f2d38a"
  peak: "#f2849c"
  subtitle-fg: "#ffffff"
  subtitle-dim: "#b9bfd4"
typography:
  display:
    fontFamily: PingFang SC
    fontSize: 22px
    fontWeight: 600
    lineHeight: 1.35
  title:
    fontFamily: PingFang SC
    fontSize: 13px
    fontWeight: 600
    letterSpacing: 0.02em
  body:
    fontFamily: PingFang SC
    fontSize: 15px
    fontWeight: 400
    lineHeight: 1.55
  body-strong:
    fontFamily: PingFang SC
    fontSize: 15px
    fontWeight: 600
    lineHeight: 1.55
  meta:
    fontFamily: PingFang SC
    fontSize: 11px
    fontWeight: 400
  caption:
    fontFamily: PingFang SC
    fontSize: 10px
    fontWeight: 400
spacing:
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
rounded:
  sm: 6px
  md: 10px
  lg: 14px
components:
  button:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.bg}"
    rounded: "{rounded.md}"
  column-card:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.lg}"
  meter-track:
    backgroundColor: "{colors.hairline}"
  meter-fill:
    backgroundColor: "{colors.accent}"
  live-dot:
    backgroundColor: "{colors.accent}"
  subtitle-bar:
    backgroundColor: "{colors.bg}"
    textColor: "{colors.subtitle-fg}"
---

## Overview

想象一间**夜里的广播字幕间**：哑光近黑的台面，只有一盏代表「正在直播」的信号灯亮着，
操作员面前是一块把双语字幕排得像读物一样干净的屏幕。这就是这个 app 该有的气质——
**安静、精确、可信**。它是干活的工具，不是要取悦你的展示品。因为它主打「完全离线、
音频不出本机」，界面更要传达**克制与稳重**，而不是花哨。少即是多：每个元素都要挣得它的像素。

## Colors

近黑的蓝灰做底（`bg`/`surface`/`panel` 三级递深），营造字幕间的沉静。**只有一个强调色**
`accent`（柔和薄荷绿）——用于「正在录制」信号点、开始键、电平表。语言色（中/英/日）克制而不刺眼，
只用来区分栏目与译文，不抢注意力。`peak` 洋红只在电平接近削波时短暂出现，是警示不是装饰。
禁止再引入第二个主色；要更多层次就调明度，不要加色相。

## Typography

一套字号阶梯，靠**字重和字号**建立层级，而不是靠颜色。译文是主角——用 `body-strong`（加粗、
最亮 `text`）；原文是配角——用 `body`、偏暗 `text-dim`。时间戳/说话人用 `meta`，控件小标签用
`caption`（`text-muted`）。字幕模式用 `display`。中文西文统一 PingFang SC，避免混排跳动。

## Layout

垂直节奏用 4/8/12/16/24 的间距阶梯，别用随手的数字。三段式：顶部操作条（含开始键 + 状态）、
左侧窄的音源控制台、右侧双语阅读区。区块之间用 `hairline` 细线或 `surface` 底色区分，
不用粗边框。阅读区留白要够，让双语文字能像书页一样呼吸。

## Elevation & Depth

不用投影（暗色界面里投影是噪音）。用**明度分层**表达深浅：`panel` < `bg` < `surface`
（越靠前越亮一点）。卡片靠 `surface` 底色和圆角浮起，不靠阴影。

## Shapes

统一圆角：小控件 `rounded.sm`，卡片 `rounded.lg`。（Tkinter 原生控件圆角能力有限，
这些 token 是设计意图，也供未来导出/重写时用；当前实现尽量以底色块和留白逼近。）

## Components

- **button**：暗底扁平，无原生高光；hover 略提亮。主操作（开始）用 `button-primary` 薄荷底、深色字。
- **column-card**：双语栏用 `surface` 卡片，标题做成小胶囊（语言色文字 + 更暗底）。
- **meter**：细横条，`hairline` 轨道 + `accent` 填充，接近满格转 `peak`。空闲也显示但更暗。
- **live-dot**：录制中在状态区显示一个 `accent` 圆点 +「录制中」。
- **subtitle-bar**：底部半透明条，译文 `display` 最亮，原文 `subtitle-dim` 小字在下。

## Do's and Don'ts

- ✅ 只用一个强调色（accent）；层级靠字重/明度/间距。
- ✅ 留白优先；宁可空，不要塞。
- ✅ 语言色只用于区分与译文，克制使用。
- ❌ 不加第二主色、不加渐变、不加投影、不加图标堆砌。
- ❌ 不用粗边框分隔；用细线或底色。
- ❌ 不让控件比内容更抢眼——内容（双语文字）永远是主角。
