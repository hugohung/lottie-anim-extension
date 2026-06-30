---
name: jd-lottie-anim-extension
description: 将两个静帧 Lottie JSON 合并为带切帧动效的 Lottie JSON，分阶段流水线架构，自动分组+弹性动效+预览模板。
version: "9.3.0"
author: honghaoxiang
agent_created: true
trigger:
  - Lottie动效
  - 合并Lottie
  - 切帧动效
  - Lottie自动动效
  - Lottie静帧合并
  - 自动生成Lottie动画
---

# Lottie 静帧合并动效 — AI 执行指南

## 一句话说明

把两张相同背景、不同前景的 Lottie 静帧图，合成一个循环播放的切换动效。

## 快速执行

```bash
python scripts/generate_merged_lottie_pipeline.py <场景A.json> <场景B.json> [输出目录]
```

**必须遵守的规则：**
1. **Python 路径**：用 `C:\Users\honghaoxiang\.workbuddy\binaries\python\versions\3.13.12\python.exe`
2. **输出目录默认值**：不传第三个参数时，输出到 `./output`
3. **运行后**：脚本自动生成预览 + 自检，无需额外操作
4. **fps 不一致处理**：两个源文件 fps 不同时，输出取 `max(A.fps, B.fps)`。源文件是静帧无动画，所有动效时长以秒定义按输出 fps 换算，30/60/100fps 下视觉节奏完全一致

## 分阶段流水线架构（核心）

脚本采用 6 阶段解耦流水线，每阶段独立函数 + 中间产物 + 自检：

| Stage | 功能 | 中间产物 | 自检内容 |
|-------|------|---------|---------|
| 0 Parse | 读 JSON · 规范化变换属性 · asset 去重 | `pipeline/00_parse.json` | 层数 · refId · 编码 |
| 1 Classify | 静态识别 · **L1分组** · 方向分配 | `pipeline/01_classify.json` | 静态配对无重复 · 每层有dir |
| 2 Timeline | 两段式交叉切换 · 错峰 · 时长区间 | `pipeline/02_timeline.json` | 时间戳递增 · 不超F_TOTAL |
| 3 Keyframes | 蓄力+overshoot+bounce · 位移+旋转+opacity策略 | `pipeline/03_keyframes.json` | kf递增 · 维度 · opacity策略 |
| 4 Assemble | 图层排序 · ind编号 · 循环锚点 · refId验证 | `merged_output.json` | refId · 循环锚点 |
| 5 Preview | 单一模板 fetch/embedded · 自检 | `preview.html` + `preview_embedded.html` | saveAs · FileSaver · 降级方案 · JSON大小 |

**局部重跑**（调参时不全跑）：
```bash
# 改时间轴 → 重跑 2-5
python generate_merged_lottie_pipeline.py --from 2 output_dir

# 改运动方式 → 重跑 3-5
python generate_merged_lottie_pipeline.py --from 3 output_dir

# 改预览 → 重跑 5
python generate_merged_lottie_pipeline.py --from 5 output_dir
```

## 动效设计（参考头图动效风格分析报告）

### 时间轴：两段式交叉切换 + 首尾空帧循环

```
t=0 (空帧)        所有元素在屏幕外
0~69f             A组元素错峰入场→展示→退场
  - 蓄力 → 飞入overshoot → bounce回弹 → 展示
69~75f            交叉窗口（A退场+B入场 同步）
75~150f           B组元素错峰入场→展示→退场
  - 蓄力 → 飞入overshoot → bounce回弹 → 展示 → 退场蓄力 → 飞出
t=150 (空帧)      所有元素回到屏幕外 → 无缝循环
─────────────────────────────────────────────────────
总时长 5.0s，30fps，150f
```

### 弹性三要素（入场和退场都有）

| 阶段 | 位移 | 旋转 | 效果 |
|------|------|------|------|
| 蓄力 | 往反方向多移 15% | 往反方向多转 30% | 预备动作 |
| overshoot | 飞过目标 8-12% | 旋转过冲 | 冲过头 |
| bounce | 回到目标位置 | 回正 | 弹性回弹 |

- 装饰类（气球/星星）：overshoot 12%，bounce 0.15s
- 文字/商品类：overshoot 8%，bounce 0.15s
- **退场也有蓄力**：先往画面内退一点 + 反向旋转，再弹射飞出
- **蓄力时长 0.08s**：按 fps 换算（100fps=8f, 30fps=2.4f），确保任何帧率下都可见

### opacity 策略（V9 教训）

| 组 | opacity | 原因 |
|----|---------|------|
| A组 | **恒定 100**（`{"a":0,"k":100}`） | 避免渲染器对 opacity=0 首帧的兼容性问题，纯靠位置控制空帧 |
| B组 | 动画（快速淡入 2-3f） | 确保交叉切换时不穿帮 |

### 缓动曲线

```python
EASE_STD_O    = {"x": [0.33], "y": [0]}       # 标准 ease-in-out 出
EASE_STD_I    = {"x": [0.67], "y": [1]}       # 标准 ease-in-out 入
EASE_BOUNCE_O = {"x": [0.167], "y": [0.1]}    # 装饰弹性出
EASE_BOUNCE_I = {"x": [0.667], "y": [1]}      # 装饰弹性入
```

### 时长区间（秒定义，按 fps 自动换算）

> **核心原则**：所有运动时长以"秒"定义，再按 fps 换算成帧。
> 这样 30fps / 60fps / 100fps 下的视觉节奏完全一致。
> V9.1 用固定帧数，100fps 下比 30fps 快 3.3 倍（蓄力 0.02s 不可见、节奏太硬）→ V9.2 修复。

```python
# 运动时长（秒定义，按 fps 自动换算）
T_IN_DUR     = (0.22, 0.35)   # 入场 0.22-0.35s
T_OUT_DUR    = (0.22, 0.45)   # 退场 0.22-0.45s
T_STAGGER    = (0.04, 0.10)   # 错峰 0.04-0.10s
T_CROSS_WIN  = 0.20           # 交叉窗口 0.20s
T_BOUNCE     = 0.15           # bounce 回弹 0.15s（偏柔，避免太硬）
T_WINDUP     = 0.08           # 蓄力 0.08s（确保任何 fps 下都可见）
T_WINDUP_MIN = 0.03           # 蓄力最小时长（退场总时长极短时的兜底下限）
T_MIN_HOLD   = 0.60           # 最小展示时长 0.6s（入场后至少停留这么久）
T_FADE_DECO  = 0.07           # 装饰类淡入淡出 0.07s
T_FADE_STD   = 0.10           # 普通/商品类淡入淡出 0.10s
```

> **验证**：30fps 和 100fps 下总时长都是 5.00s，交叉窗口都是 2.30→2.50s，蓄力 0.067s vs 0.080s（帧取整微小差异，视觉无感知）。

## 分组策略（L1/L2/L3 三层）

| 层次 | 手段 | 触发时机 | 成本 |
|------|------|---------|------|
| **L1** | 纯代码自动分组 | 默认每次跑 | 零 |
| **L2** | 人工对话微调 | 看预览发现不对 | 1轮对话 |
| **L3** | AI看图层小图 | L1+L2搞不定 | 较高token（后期启用） |

### L1 纯代码分组规则（按优先级）

1. **图层名语义匹配** → 同类整组（text/decoration 关键词字典）
2. **空间聚类** → 距离相近的归一组（阈值 = 画布宽度 × 25%）
3. **兜底** → 没匹配上的各自独立

组方向由组中心坐标决定，同组所有元素共享方向。

### L2 人工微调

在输出目录建 `group_config.json`：
```json
{"overrides": [{"layers": ["榴莲.png","可乐.png","立白.png","奶粉.png"], "dir": "right"}]}
```
只写需要调整的组，其余走 L1 默认。然后 `--from 2` 重跑。

### L3 看图分组（后期启用）

- Stage 0 导出每个 image 图层为 PNG → `layer_thumbs/`
- 生成 `layer_manifest.json`（图层名+坐标+小图路径）
- AI 看小图+坐标做分组，输出 group_config.json
- 矢量图层后续支持 shapes 转 SVG

## 预览模板（固化管理）

### 两种模式

| 模式 | 文件 | 数据加载 | 使用方式 |
|------|------|---------|---------|
| fetch | `preview.html` | `fetch('merged_output.json')` | 需启动 `python -m http.server` |
| embedded | `preview_embedded.html` | JSON 直接内嵌 | 双击打开，无需服务器 |

### 预览功能

- ▶/⏸ 播放/暂停切换
- 🔄 重播
- ⏩ 0.5x / 1x / 2x 速度切换
- 📥 下载 JSON（FileSaver.js `saveAs()` + 双CDN + 降级方案）
- 📊 实时帧数 + JSON 文件大小显示（`帧: 75 / 150  |  JSON: 226 KB`，≥1MB 自动切 MB）

### 模板固化方案

1. **单一模板函数** `build_preview_html(mode, json_str)` — fetch/embedded 共用同一份 CSS+JS
2. **生成后自检** — 检查 saveAs/FileSaver/loadFileSaver/createObjectURL/__JSON_SIZE__，缺任一项报错退出
3. **禁止手写预览 HTML** — 必须从脚本生成

## 输出物

```
输出目录/
├── merged_output.json          # 合并后的 Lottie 动效文件 ← 最终产物
├── preview.html                # 预览（fetch 模式，需 HTTP 服务器）
├── preview_embedded.html       # 预览（内嵌模式，双击打开）
├── pipeline/                   # 中间产物（可打开检查）
│   ├── 00_parse.json
│   ├── 01_classify.json
│   ├── 02_timeline.json
│   └── 03_keyframes.json
└── group_config.json           # 人工微调覆盖（可选，L2）
```

## 绝对禁止做的事

| 禁止项 | 原因 | 正确做法 |
|--------|------|----------|
| 关键帧设 `h:1` | 渲染器不插值 | 不设置 h 属性 |
| position 用 2 元素 | lottie-web 崩溃 | 始终 3 元素 `[x,y,z=0]` |
| IIFE 包裹 JS 函数 | onclick 访问不到 | 全局声明 |
| 硬编码版本号/帧率/尺寸 | 不同源文件不同 | 从源文件读取 |
| A组 opacity 设 0 首帧 | 渲染器兼容性问题 | A组恒定 100，位置控制空帧 |
| 手写预览 HTML | 丢 FileSaver 逻辑 | 必须从 `build_preview_html()` 生成 |

## 自定义配置

编辑 `scripts/generate_merged_lottie_pipeline.py` 中的 `stage_timeline()` 参数：

```python
p = {
    'T_TOTAL': 5.0,        # 总时长（秒）
    'T_A_IN_START': 0.0,   # A组入场开始（秒）
    'T_CROSS': 2.4,        # 交叉切换中心点（秒）
    'T_CROSS_WIN': 0.20,   # 交叉窗口时长（秒，按 fps 换算）
}
```

编辑脚本顶部的时长常量（秒定义，自动适配 fps）：
```python
T_IN_DUR     = (0.22, 0.35)   # 入场
T_OUT_DUR    = (0.22, 0.45)   # 退场
T_BOUNCE     = 0.15           # bounce 回弹
T_WINDUP     = 0.08           # 蓄力
T_WINDUP_MIN = 0.03           # 蓄力下限（退场极短时兜底）
T_MIN_HOLD   = 0.60           # 最小展示时长
T_FADE_DECO  = 0.07           # 装饰类淡入淡出
T_FADE_STD   = 0.10           # 普通类淡入淡出
```

弹性比例（在 `_build_pos_kfs_style` / `_build_rot_kfs_style` 中）：
```python
os_ratio = 0.12 if is_deco else 0.08    # overshoot 幅度
windup_ratio = 0.15                       # 蓄力幅度
```

## 目录结构

```
jd-lottie-anim-extension/
├── SKILL.md                                  # 本文件
├── README.md
├── LICENSE
├── scripts/
│   ├── generate_merged_lottie_pipeline.py    # 主脚本（流水线版 V9.3）
│   ├── generate_merged_lottie.py             # 旧版脚本（V8，保留备用）
│   ├── generate_merged_lottie_pipeline_v9.2_backup.py  # V9.2 备份
│   ├── generate_merged_lottie_pipeline_v8.1_backup.py  # V8.1 备份
│   └── generate_merged_lottie_v8_backup.py   # V8 旧版备份
├── references/
│   ├── .gitkeep
│   └── LOTTIE_BUG_CHECKLIST.md
└── examples/
    ├── .gitkeep
    ├── scene-a.json
    ├── scene-b.json
    ├── expected-output.json
    └── preview.html
```

## 版本历史

| 版本 | 日期 | 主要变更 |
|------|------|----------|
| V9.3 | 2026-06-30 | **全面fps自适应**：退场蓄力下限max(2帧)改T_WINDUP_MIN秒定义；淡入淡出0.07/0.10硬编码改T_FADE常量；最小展示0.6硬编码改T_MIN_HOLD常量；废弃_calc_stagger_ref改秒定义；30fps+100fps交叉验证节奏一致 |
| V9.2 | 2026-06-30 | **fps自适应**：所有时长改秒定义按fps换算（修复100fps下节奏太快/蓄力不可见）；**静态识别**：改用asset尺寸(aw/ah)代替base64比对（修复lottielab重导出导致的漏判）；**分组bug**：_auto_group直接设图层属性（修复空名nm做dict key互相覆盖导致所有元素同方向） |
| V9.1 | 2026-06-30 | 分阶段流水线架构（6阶段解耦+中间产物+自检+局部重跑）；参考动效风格（两段式交叉+首尾空帧+蓄力+overshoot+bounce+退场蓄力）；L1纯代码分组（语义匹配+空间聚类）+L2人工微调；预览模板固化（单一函数+自检+JSON大小显示） |
| V8.1 | 2026-06-30 | 预览模板固化：fetch/embedded 合并 build_preview_html()，生成后自检 |
| V8.0 | 2026-06-29 | 预览优化：toggle按钮、FileSaver.js、回归V6时间轴 |
| V9实验 | 2026-06-29 | 参考动效分析尝试，因元素不可见回滚（教训：A组opacity别设0首帧） |
| V7.x | 2026-06-24~26 | 7维度静态识别、center交叉溶解、CDN修复 |
| V6 | 2026-06-18 | position/scale 3分量、视觉边界飞行距离、弹性缓动 |
| V1-V5 | 2026-06-17~18 | 基础功能、parent保留、anchor/rotation/cl修复 |

## 排错速查

| 症状 | 最可能原因 | 解决方法 |
|------|-----------|---------|
| 预览白屏 | CDN 加载失败 | 检查网络；预览页有双CDN兜底 |
| 按钮无效 | JS 函数在 IIFE 内 | 确认所有函数是全局声明 |
| 闪烁 | 循环衔接缺锚点 | t=F_TOTAL 处补入首帧锚点（脚本自动处理） |
| 元素位置偏移 | anchor 丢失或2元素position | 检查源文件anchor、position是否3元素 |
| 下载跳转网页 | 丢 FileSaver 逻辑 | 已固化：单一模板+自检，不会丢 |
| 背景消失 | 背景被识别为前景 | 检查两源文件背景层位置/内容是否匹配 |
| 同组元素方向不一致 | L1分组不准 | 用 group_config.json 手工微调，`--from 2` 重跑 |
| 元素不可见 | A组opacity设了0首帧 | A组必须恒定100，位置控制空帧 |
| B组退场被截断 | out_end 超过 F_TOTAL | assign_timeline 的 max_out 约束 |
| **节奏太快/太硬** | 非30fps时固定帧数没缩放 | V9.2已修：时长全用秒定义，按fps自动换算 |
| **蓄力看不见** | 蓄力时长固定2f，高fps下<0.03s | V9.2已修：T_WINDUP=0.08s，按fps换算 |
| **静态层被赋予动效** | base64不一致导致漏判 | V9.2已修：改用 asset尺寸(aw/ah) 判断静态 |
| **所有元素同方向** | 图层名为空时nm做dict key互相覆盖 | V9.2已修：_auto_group直接设图层属性 |

## 备份与回滚

| 版本 | 备份文件 | 回滚命令 |
|------|---------|---------|
| V9.2 流水线 | `generate_merged_lottie_pipeline_v9.2_backup.py` | `cp generate_merged_lottie_pipeline_v9.2_backup.py generate_merged_lottie_pipeline.py` |
| V8.1 流水线 | `generate_merged_lottie_pipeline_v8.1_backup.py` | `cp generate_merged_lottie_pipeline_v8.1_backup.py generate_merged_lottie_pipeline.py` |
| V8 旧版 | `generate_merged_lottie_v8_backup.py` | `cp generate_merged_lottie_v8_backup.py generate_merged_lottie.py` |
