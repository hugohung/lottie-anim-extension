---
name: 规范化Lottie动效延展
description: 将两个静帧 Lottie JSON 合并为带切帧动效的 Lottie JSON，自动识别静态图层并生成平滑的入场/退场动画。
trigger:
  - Lottie动效
  - 合并Lottie
  - 切帧动效
  - Lottie自动动效
  - Lottie静帧合并
  - 自动生成Lottie动画
---

# Lottie 静帧合并动效 Skill

## 功能描述

将两个相同背景、不同前景的 Lottie 静帧 JSON 文件，自动合并为一个带有平滑切帧动效的 Lottie JSON。

**核心能力：**
1. 自动识别静态图层（背景、腰封等不变元素）
2. 自动识别前景变化图层
3. 生成专业的入场/退场动画（支持左/右/下/中方向）
4. 保留图层父子关系（parent）
5. 保留旋转、锚点、混合模式等属性
6. 输出可直接在 Lottie 播放器播放的 JSON

## 使用方法

### 基本用法

```bash
python generate_merged_lottie.py <文件A.json> <文件B.json> [输出目录]
```

**参数说明：**
- `文件A.json`：第一个静帧 Lottie 文件（通常是初始状态）
- `文件B.json`：第二个静帧 Lottie 文件（通常是变化后状态）
- `输出目录`（可选）：输出文件存放目录，默认为脚本所在目录

**示例：**

```bash
# 使用绝对路径
python generate_merged_lottie.py "C:/Users/honghaoxiang/Desktop/文件A.json" "C:/Users/honghaoxiang/Desktop/文件B.json" "H:/workbuddy-ziliao/output"

# 使用相对路径
python generate_merged_lottie.py ./scene1.json ./scene2.json ./output
```

### 输出文件

运行后会在输出目录生成：

1. **merged_output.json** - 合并后的 Lottie 动效文件
2. **preview.html** - 本地预览页面（自动生成）

## 工作流程

### 1. 准备输入文件

**要求：**
- 两个文件必须是相同尺寸（宽高一致）
- 背景层必须完全相同（图层名、位置、锚点、缩放一致）
- 前景元素可以有不同位置、不同内容

**典型场景：**
- 电商会场头图：背景固定，优惠信息变化
- 广告横幅：背景固定，文案/商品变化
- UI 动效：静态框架 + 动态内容切换

### 2. 运行脚本

```bash
python generate_merged_lottie.py "场景A.json" "场景B.json" "输出目录"
```

**脚本会自动：**
1. 读取并解析两个 JSON 文件
2. 合并资产（自动去重）
3. 识别静态图层（背景、腰封等）
4. 识别前景变化图层
5. 为每个前景图层生成入场/退场关键帧
6. 构建输出 JSON
7. 生成预览 HTML

### 3. 预览和导出

**预览：**
- 打开 `preview.html` 在浏览器中预览
- 支持播放/暂停/重播
- 支持 0.5x / 1x / 2x 速度切换
- 显示当前帧数

**导出：**
- 点击"下载 JSON"按钮下载 `merged_output.json`
- 或直接从输出目录复制 `merged_output.json`

**在 Lottie 中使用：**
- 将 `merged_output.json` 导入 Lottie 播放器
- 或集成到网页/APP 中

## 动画时间轴

默认时间轴设置（可在脚本中修改）：

```
0-5f      场景 A 静置
5-15f     场景 A 退场 + 场景 B 入场
15-90f    场景 B 静置
90-100f   场景 B 退场 + 场景 A 入场
100-150f  场景 A 静置（循环点）
```

**总时长：** 150 帧（5秒 @ 30fps）

## 高级配置

### 修改时间轴

编辑 `generate_merged_lottie.py` 中的配置部分：

```python
# 时间轴（帧）
SCENE_A_HOLD_END     = 5
SCENE_A_EXIT_START   = 5
SCENE_A_EXIT_END     = 15
SCENE_B_ENTRY_START  = 10
SCENE_B_ENTRY_END    = 20
SCENE_B_HOLD_END     = 90
SCENE_B_EXIT_START   = 90
SCENE_B_EXIT_END     = 100
SCENE_A_ENTRY_START  = 95
SCENE_A_ENTRY_END    = 105
TOTAL_FRAMES         = 150
```

### 修改画布尺寸

```python
CANVAS_W = 1125  # 画布宽度
CANVAS_H = 566   # 画布高度
FPS = 30          # 帧率
```

### 修改缓动曲线

```python
EASE_OUT   = {"x": [0.333], "y": [0]}   # 标准缓出
EASE_IN    = {"x": [0.667], "y": [1]}   # 标准缓入
EASE_SNAPPY_I = {"x": [0.667], "y": [0.667]}  # 弹性缓入
EASE_SNAPPY_O = {"x": [0.333], "y": [0.333]}  # 弹性缓出
```

## 技术细节

### 静态图层识别规则

两个文件的图层满足以下**所有条件**时，被认为是静态图层：

1. 图层名（`nm`）相同
2. 父级图层（`parent`）相同
3. 旋转角度（`rotation`）相同（容差 < 0.01）
4. 位置（`position`）相同（容差 < 0.1px）
5. 锚点（`anchor`）相同（容差 < 0.1px）
6. 缩放（`scale`）相同（容差 < 0.1%）

### 前景图层动画规则

**入场动画：**
- 元素从画布外飞入
- 方向自动判断（左/右/下/中）
- 有弹性过冲效果
- 支持错帧延迟（同方向元素依次延迟 3 帧）

**退场动画：**
- 元素飞出画布
- 同步淡出
- 与另一侧入场动画重叠 5 帧

### 图层排序规则

输出 JSON 的图层数组顺序（Lottie 规范：数组首位 = 最上层）：

```
[0..n]       静态顶层（腰封等，永远最上）
[n+1..m]    场景 B 前景（B 盖住 A）
[m+1..k]    场景 A 前景
[k+1..end]  静态底层（背景，永远最下）
```

### 父子关系（Parent）处理

- 保留源文件的 `parent` 属性
- 重新编号后自动 remap `parent` 引用
- 跨场景的父子关系会自动处理

## 支持的文件格式

**输入：**
- Lottie JSON 格式（.json）
- 支持图像图层（ty=2）
- 支持形状图层（ty=4）
- 支持合成图层（ty=0）
- 支持空对象（ty=3）

**输出：**
- Lottie JSON 格式（.json）
- 兼容 Lottie 5.6.10+
- 支持 SVG / Canvas / HTML 渲染

## 常见问题

### Q: 提示 "KeyError: ('a', '')" 怎么办？

**A:** 某些图层没有 `refId`。已修复，脚本现在会自动处理无 `refId` 的图层。

### Q: 生成的动画位置不对？

**A:** 检查源文件的锚点（`anchor`）是否正确。锚点影响图层的旋转/缩放中心。

### Q: 如何调整动画速度？

**A:** 
- 预览时点击 0.5x / 1x / 2x 按钮
- 或修改 `FPS` 和 `TOTAL_FRAMES` 配置

### Q: 如何添加更多动效效果？

**A:** 修改 `build_pos_kfs()` 和 `build_opacity_kfs()` 函数，自定义关键帧生成逻辑。

## 自查表

生成后务必检查以下项目（详细见 `LOTTIE_BUG_CHECKLIST.md`）：

- [ ] 图层元数据完整（position/anchor/scale/rotation/parent/cl）
- [ ] 静态图层识别准确（无漏识/误识）
- [ ] 关键帧按 `t` 升序排列
- [ ] 无 `h:1` 关键帧（会导致不插值）
- [ ] 退场 opacity 已生成（不受 initial_visible 影响）
- [ ] 飞行距离基于视觉边界计算
- [ ] 图层排序正确（背景在末尾）
- [ ] parent 已 remap
- [ ] 资产已去重
- [ ] 首尾帧状态一致（可循环）

## 文件和目录结构

```
lottie-merge-anim/
├── SKILL.md                    # Skill 说明文档
├── generate_merged_lottie.py   # 主脚本
├── LOTTIE_BUG_CHECKLIST.md    # AI 自查表
└── examples/                   # 示例文件（可选）
    ├── example1-A.json
    ├── example1-B.json
    └── example1-output.json
```

## 版本历史

**V4 (2026-06-18):**
- 支持命令行参数输入文件
- 支持无 `refId` 的图层类型（形状图层等）
- 改进错误处理

**V3 (2026-06-17):**
- 保留 parent 父子关系
- 保留 rotation 旋转属性
- 保留 cl 混合模式
- 修复图层排序
- 修复锚点丢失

**V2 (2026-06-17):**
- 修复关键帧排序
- 移除 `h:1` 属性
- 修复飞行距离计算

**V1 (2026-06-17):**
- 初始版本
- 基本合并功能
- 静态图层识别

## 授权和引用

- 本 Skill 由 WorkBuddy 生成
- 使用 Lottie 开源格式
- lottie-web: https://github.com/airbnb/lottie-web

---

**有问题或建议？** 请联系 honghaoxiang 或提交 GitHub issue。
