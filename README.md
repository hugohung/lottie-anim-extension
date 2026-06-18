# 规范化Lottie动效延展

> WorkBuddy Skill — 将两个静帧 Lottie JSON 合并为带切帧动效的 Lottie JSON，自动识别静态图层并生成平滑的入场/退场动画。

## 功能特性

- 🎯 自动识别静态图层（背景、腰封等不变元素）
- ✨ 自动识别前景变化图层
- 🎬 生成专业的入场/退场动画（支持左/右/下/中方向）
- 🔗 保留图层父子关系（parent）
- 📐 保留旋转、锚点、混合模式等属性
- 📦 输出可直接在 Lottie 播放器播放的 JSON

## 安装方式

### WorkBuddy 用户

1. 下载 [Release zip](../../releases/latest)
2. 在 WorkBuddy 技能管理 → 上传技能，选择 zip 文件

### 从源码安装

```bash
git clone https://github.com/hugohung/workbuddy-skill-lottie-merge-anim.git ~/.workbuddy/skills/lottie-merge-anim
```

## 使用方式

在 WorkBuddy 对话中直接说：
> "帮我合并这两个 Lottie 文件"

或手动运行：

```bash
python generate_merged_lottie.py <文件A.json> <文件B.json> [输出目录]
```

**示例：**

```bash
python generate_merged_lottie.py "C:/Users/honghaoxiang/Desktop/文件A.json" "C:/Users/honghaoxiang/Desktop/文件B.json" "H:/workbuddy-ziliao/output"
```

## 工作流程

1. 准备两个相同尺寸、相同背景、不同前景的 Lottie 静帧 JSON
2. 运行脚本自动合并
3. 打开生成的 `preview.html` 预览动效
4. 下载 `merged_output.json` 使用

## 动画时间轴

```
0-5f      场景 A 静置
5-15f     场景 A 退场 + 场景 B 入场
15-90f    场景 B 静置
90-100f   场景 B 退场 + 场景 A 入场
100-150f  场景 A 静置（循环点）
```

## License

MIT License
