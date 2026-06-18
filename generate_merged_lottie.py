"""
V4: 将两个静帧 Lottie JSON 合并为一个带切帧动效的 Lottie JSON。

核心改进:
1. 精确记录每个图层的完整元数据（name, position, anchor, scale, ind）
2. 对比识别静态图层（同名且position/anchor/scale完全相同的）= 不动效
3. 保留源文件图层原始排序（ind）和锚点（anchor）
4. 只对变化的图层编写入场/退场动效
5. 图层排列：顶层静态(腰封) → Scene B前景 → Scene A前景 → 底层静态(背景)
6. 支持命令行参数输入文件
"""
import json
import copy
import os
import sys

# ─── 命令行参数 ──────────────────────────────────────
def print_usage():
    print("用法:")
    print("  python generate_merged_lottie.py <文件A.json> <文件B.json> [输出目录]")
    print("")
    print("示例:")
    print("  python generate_merged_lottie.py scene1.json scene2.json ./output")
    sys.exit(1)

if len(sys.argv) < 3:
    print_usage()

FILE_A = sys.argv[1]
FILE_B = sys.argv[2]
OUTPUT_DIR = sys.argv[3] if len(sys.argv) > 3 else os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(OUTPUT_DIR, "merged_output.json")

print(f"输入文件 A: {FILE_A}")
print(f"输入文件 B: {FILE_B}")
print(f"输出目录: {OUTPUT_DIR}")
print("")

# 画布尺寸（从源文件动态读取，见下方）
FPS = 30

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

# 缓动
EASE_OUT   = {"x": [0.333], "y": [0]}
EASE_IN    = {"x": [0.667], "y": [1]}
EASE_SNAPPY_I = {"x": [0.667], "y": [0.667]}
EASE_SNAPPY_O = {"x": [0.333], "y": [0.333]}

# ─── 读取源文件 ────────────────────────────────────────
with open(FILE_A, 'r', encoding='utf-8') as f:
    src_a = json.load(f)
with open(FILE_B, 'r', encoding='utf-8') as f:
    src_b = json.load(f)

# 画布尺寸从源文件读取（不再硬编码）
CANVAS_W = src_a.get('w', 1125)
CANVAS_H = src_a.get('h', 566)

# ─── 合并 assets（base64 签名去重）─────────────────────
assets = []
asset_map = {}  # (source_tag, original_refId) -> new_refId
asset_sigs = {}

def add_asset(asset, source_tag):
    """添加资产，基于内容签名去重（兼容图片asset和comp合成asset）"""
    if 'id' not in asset:
        return  # 跳过没有id的资产

    w = asset.get('w', 0)
    h = asset.get('h', 0)
    p = asset.get('p', '')
    # comp 类型 asset 用 layers 内容做签名，图片类型用 base64 前100字符
    if 'layers' in asset:
        layer_count = len(asset['layers'])
        first_layer_nm = asset['layers'][0].get('nm', '') if asset['layers'] else ''
        sig = ('comp', w, h, layer_count, first_layer_nm)
    else:
        sig = ('img', w, h, p[:100] if p else '')
    
    if sig not in asset_sigs:
        prefix = 'comp' if 'layers' in asset else 'image'
        new_id = f"{prefix}_{len(assets)}"
        new_asset = copy.deepcopy(asset)
        new_asset['id'] = new_id
        assets.append(new_asset)
        asset_sigs[sig] = new_id
        asset_map[(source_tag, asset['id'])] = new_id
    else:
        asset_map[(source_tag, asset['id'])] = asset_sigs[sig]

# 只处理有id的asset
for a in src_a.get('assets', []):
    if 'id' in a:
        add_asset(a, 'a')
for a in src_b.get('assets', []):
    if 'id' in a:
        add_asset(a, 'b')

print(f"Total unique assets: {len(assets)}")

# ─── 提取完整图层元数据 ────────────────────────────────
def extract_layer_meta(layer, source_tag):
    """提取图层的所有关键元数据，用于比对和重建"""
    ks = layer.get('ks', {})
    p = ks.get('p', {}).get('k', [0, 0, 0])  # position
    a = ks.get('a', {}).get('k', [0, 0, 0])  # anchor
    s = ks.get('s', {}).get('k', [100, 100, 100])  # scale
    o = ks.get('o', {}).get('k', 100)  # opacity
    r = ks.get('r', {}).get('k', 0)  # rotation
    
    refId = layer.get('refId', '')
    layer_type = layer.get('ty', 2)

    # 映射 refId（支持图像图层ty=2 和 预合成图层ty=0）
    new_refid = refId
    aw, ah = 100, 100

    if refId and (layer_type in (0, 2)):
        if (source_tag, refId) in asset_map:
            new_refid = asset_map[(source_tag, refId)]
        # 获取 asset 原始尺寸
        for asset in (src_a['assets'] if source_tag == 'a' else src_b['assets']):
            if asset['id'] == refId:
                aw, ah = asset.get('w', 100), asset.get('h', 100)
                break
    
    return {
        'name': layer.get('nm', 'Unknown'),
        'ind': layer.get('ind', 0),
        'layer_type': layer_type,
        'refId': new_refid,
        'orig_refId': refId,
        'position': [p[0] if isinstance(p, list) else 0, p[1] if isinstance(p, list) else 0],
        'anchor': [a[0] if isinstance(a, list) else 0, a[1] if isinstance(a, list) else 0],
        'scale': [s[0] if isinstance(s, list) else s, s[1] if isinstance(s, list) else s, s[2] if isinstance(s, list) and len(s) > 2 else 100],
        'opacity': o if isinstance(o, (int, float)) else 100,
        'rotation': r if isinstance(r, (int, float)) else 0,
        'parent': layer.get('parent'),  # 父级图层 ind
        'cl': layer.get('cl', ''),       # 混合模式/样式
        'asset_w': aw,
        'asset_h': ah,
        'source_tag': source_tag,
    }

# 提取两个文件的所有图层
layers_a = [extract_layer_meta(l, 'a') for l in src_a['layers']]
layers_b = [extract_layer_meta(l, 'b') for l in src_b['layers']]

print(f"\n=== 第一个画面 ({len(layers_a)} layers) ===")
for la in layers_a:
    print(f"  ind={la['ind']:2d}  {la['name']:20s}  pos=[{la['position'][0]:.0f},{la['position'][1]:.0f}]  anchor=[{la['anchor'][0]:.0f},{la['anchor'][1]:.0f}]  scale=[{la['scale'][0]:.0f}%,{la['scale'][1]:.0f}%]  asset={la['asset_w']}x{la['asset_h']}")

print(f"\n=== 第二个画面 ({len(layers_b)} layers) ===")
for lb in layers_b:
    print(f"  ind={lb['ind']:2d}  {lb['name']:20s}  pos=[{lb['position'][0]:.0f},{lb['position'][1]:.0f}]  anchor=[{lb['anchor'][0]:.0f},{lb['anchor'][1]:.0f}]  scale=[{lb['scale'][0]:.0f}%,{lb['scale'][1]:.0f}%]  asset={lb['asset_w']}x{lb['asset_h']}")

# ─── 识别静态图层 ──────────────────────────────────────
# 同名 + position/anchor/scale 完全相同 = 静态图层
def layer_same(la, lb):
    return (
        la['name'] == lb['name']
        and la['parent'] == lb['parent']
        and abs(la['rotation'] - lb['rotation']) < 0.01
        and abs(la['position'][0] - lb['position'][0]) < 0.1
        and abs(la['position'][1] - lb['position'][1]) < 0.1
        and abs(la['anchor'][0] - lb['anchor'][0]) < 0.1
        and abs(la['anchor'][1] - lb['anchor'][1]) < 0.1
        and abs(la['scale'][0] - lb['scale'][0]) < 0.1
        and abs(la['scale'][1] - lb['scale'][1]) < 0.1
    )

static_layers_a = []  # 场景A中的静态图层
static_layers_b = []  # 场景B中的静态图层
fg_layers_a = []      # 场景A中的前景（会动）
fg_layers_b = []      # 场景B中的前景（会动）

for la in layers_a:
    is_static = False
    for lb in layers_b:
        if layer_same(la, lb):
            is_static = True
            # 使用场景A的元数据作为静态图层（去重）
            static_layers_a.append(la)
            static_layers_b.append(lb)
            break
    if not is_static:
        fg_layers_a.append(la)

for lb in layers_b:
    is_static = False
    for la in layers_a:
        if layer_same(la, lb):
            is_static = True
            break
    if not is_static:
        fg_layers_b.append(lb)

print(f"\n=== Static layers ({len(static_layers_a)}) ===")
for sl in static_layers_a:
    print(f"  {sl['name']:20s}  pos=[{sl['position'][0]:.0f},{sl['position'][1]:.0f}]  anchor=[{sl['anchor'][0]:.0f},{sl['anchor'][1]:.0f}]")

print(f"\n=== Scene A foreground ({len(fg_layers_a)} layers) ===")
for fl in fg_layers_a:
    print(f"  {fl['name']:20s}  pos=[{fl['position'][0]:.0f},{fl['position'][1]:.0f}]  anchor=[{fl['anchor'][0]:.0f},{fl['anchor'][1]:.0f}]")

print(f"\n=== Scene B foreground ({len(fg_layers_b)} layers) ===")
for fl in fg_layers_b:
    print(f"  {fl['name']:20s}  pos=[{fl['position'][0]:.0f},{fl['position'][1]:.0f}]  anchor=[{fl['anchor'][0]:.0f},{fl['anchor'][1]:.0f}]")

# ─── 确定入场方向 ──────────────────────────────────────
def get_entry_direction(x, y):
    """根据锚点位置推断入场方向"""
    if y > 500:
        return "bottom"
    if x < 400:
        return "left"
    if x > 700:
        return "right"
    return "center"

for fl in fg_layers_a:
    fl['direction'] = get_entry_direction(fl['position'][0], fl['position'][1])
for fl in fg_layers_b:
    fl['direction'] = get_entry_direction(fl['position'][0], fl['position'][1])

print("\n=== Entry directions ===")
for fl in fg_layers_a:
    print(f"  A: {fl['name']:20s} → {fl['direction']}")
for fl in fg_layers_b:
    print(f"  B: {fl['name']:20s} → {fl['direction']}")

# ─── 计算飞行距离 ──────────────────────────────────────
def get_flight_distance(pos_x, pos_y, direction, asset_w, asset_h, anchor_x, anchor_y, scale_x, scale_y):
    """
    基于元素视觉边界计算飞行偏移量。
    visual_left  = pos_x - anchor_x * scale_x
    visual_top   = pos_y - anchor_y * scale_y
    visual_right = visual_left + asset_w * scale_x
    visual_bottom= visual_top  + asset_h * scale_y
    """
    vl = pos_x - anchor_x * scale_x
    vt = pos_y - anchor_y * scale_y
    vr = vl + asset_w * scale_x
    vb = vt + asset_h * scale_y
    
    margin = 80

    if direction == "left":
        dx_in = (-margin) - vr  # 视觉右边缘移出左边界
        return (dx_in, 0), (-dx_in * 0.06, 0)
    elif direction == "right":
        dx_in = (CANVAS_W + margin) - vl  # 视觉左边缘移出右边界
        return (dx_in, 0), (-dx_in * 0.06, 0)
    elif direction == "bottom":
        dy_in = (CANVAS_H + margin) - vt  # 视觉上边缘移出下边界
        return (0, dy_in), (0, -dy_in * 0.06)
    elif direction == "top":
        dy_in = (-margin) - vb  # 视觉下边缘移出上边界
        return (0, dy_in), (0, -dy_in * 0.06)
    else:
        return (0, 0), (0, 0)

# ─── 构建关键帧 ────────────────────────────────────────
def make_kf(t, value, ei, eo):
    if isinstance(value, (int, float)):
        value = [value]
    return {
        "i": {"x": ei["x"][:], "y": ei["y"][:]},
        "o": {"x": eo["x"][:], "y": eo["y"][:]},
        "t": t,
        "s": value[:],
    }

def build_pos_kfs(fl, delay, entry_start, exit_start, entry_end, exit_end, initial_visible):
    x, y = fl['position'][0], fl['position'][1]
    ax, ay = fl['anchor'][0], fl['anchor'][1]
    sx = fl['scale'][0] / 100.0
    sy = fl['scale'][1] / 100.0
    
    (dx_in, dy_in), (dx_os, dy_os) = get_flight_distance(
        x, y, fl['direction'],
        fl['asset_w'], fl['asset_h'],
        ax, ay, sx, sy
    )
    
    entry_x = x + dx_in
    entry_y = y + dy_in
    overshoot_x = x + dx_os if fl['direction'] != "center" else x
    overshoot_y = y + dy_os if fl['direction'] != "center" else y
    
    kfs = []
    
    # t=0
    if initial_visible:
        kfs.append(make_kf(0, [x, y], EASE_OUT, EASE_OUT))
    else:
        kfs.append(make_kf(0, [entry_x, entry_y], EASE_OUT, EASE_OUT))
    
    # 退场
    t_exit_start = exit_start
    t_exit_end = t_exit_start + 8
    kfs.append(make_kf(t_exit_start, [x, y], EASE_IN, EASE_OUT))
    kfs.append(make_kf(t_exit_end, [entry_x, entry_y], EASE_OUT, EASE_OUT))
    
    # 入场
    t_entry = entry_start + delay
    t_entry_end = t_entry + 8
    t_bounce = t_entry_end + 6
    kfs.append(make_kf(t_entry, [entry_x, entry_y], EASE_IN, EASE_OUT))
    kfs.append(make_kf(t_entry_end, [overshoot_x, overshoot_y], EASE_SNAPPY_I, EASE_SNAPPY_O))
    kfs.append(make_kf(t_bounce, [x, y], EASE_OUT, EASE_OUT))
    
    kfs.sort(key=lambda kf: kf['t'])
    return kfs

def build_opacity_kfs(entry_start, exit_start, entry_end, exit_end, delay, initial_visible):
    kfs = []
    
    kfs.append(make_kf(0, [100] if initial_visible else [0], EASE_OUT, EASE_OUT))
    
    # 退场淡出
    t_out_start = exit_start
    t_out_end = t_out_start + 5
    kfs.append(make_kf(t_out_start, [100], EASE_IN, EASE_IN))
    kfs.append(make_kf(t_out_end, [0], EASE_IN, EASE_IN))
    
    # 入场淡入
    t_in_start = entry_start + delay
    t_in_end = t_in_start + 5
    kfs.append(make_kf(t_in_start, [0], EASE_OUT, EASE_OUT))
    kfs.append(make_kf(t_in_end, [100], EASE_OUT, EASE_OUT))
    
    kfs.sort(key=lambda kf: kf['t'])
    return kfs

# ─── 为前景图层生成关键帧 ──────────────────────────────
def build_fg_keyframes(fg_list, entry_start, entry_end, exit_start, exit_end, initial_visible):
    """为前景图层生成动效关键帧，保留原始 ind 排序"""
    # 按原始 ind 排序（保持图层前后关系）
    sorted_fg = sorted(fg_list, key=lambda fl: fl['ind'])
    
    # 错帧：同方向元素依次延迟 3 帧
    dir_delay = {}
    base_delays = {"left": 0, "right": 1, "bottom": 2, "center": 3, "top": 0}
    
    result = []
    for fl in sorted_fg:
        d = fl['direction']
        delay = dir_delay.get(d, base_delays.get(d, 0))
        dir_delay[d] = delay + 3
        
        result.append({
            'name': fl['name'],
            'ind': fl['ind'],
            'refId': fl['refId'],
            'position': fl['position'],
            'anchor': fl['anchor'],
            'scale': fl['scale'],
            'direction': fl['direction'],
            'layer_type': fl['layer_type'],
            'rotation': fl['rotation'],
            'parent': fl['parent'],
            'cl': fl['cl'],
            'source_tag': fl['source_tag'],
            'delay': delay,
            'pos_kfs': build_pos_kfs(fl, delay, entry_start, exit_start, entry_end, exit_end, initial_visible),
            'opacity_kfs': build_opacity_kfs(entry_start, exit_start, entry_end, exit_end, delay, initial_visible),
        })
    return result

scene_a_kfs = build_fg_keyframes(fg_layers_a,
    SCENE_A_ENTRY_START, SCENE_A_ENTRY_END,
    SCENE_A_EXIT_START, SCENE_A_EXIT_END,
    initial_visible=True)

scene_b_kfs = build_fg_keyframes(fg_layers_b,
    SCENE_B_ENTRY_START, SCENE_B_ENTRY_END,
    SCENE_B_EXIT_START, SCENE_B_EXIT_END,
    initial_visible=False)

# ─── 构建输出 Lottie JSON ──────────────────────────────
output = {
    "v": "5.6.10",
    "fr": FPS,
    "ip": 0,
    "op": TOTAL_FRAMES,
    "w": CANVAS_W,
    "h": CANVAS_H,
    "nm": "Merged Animation V3",
    "ddd": 0,
    "assets": assets,
    "layers": []
}

def make_layer(el, animated=True):
    """构造 Lottie 图层"""
    # rotation：有动画的图层保留原始旋转值，没有的取 meta 中的 rotation
    rot = el.get('rotation', 0)
    # cl 属性（混合模式）
    cl = el.get('cl', '')
    # parent（先保留原始值，后续 remap）
    parent = el.get('parent')
    # 保存原始 ind（用于后续 parent remap）
    source_ind = el['ind']
    source_tag = el.get('source_tag', '')
    refId = el.get('refId', '')
    
    base = {
        "ddd": 0,
        "ind": len(output['layers']),  # 临时值，后面重新编号
        "ty": el['layer_type'],
        "nm": el['name'],
        "sr": 1,
        "ao": 0,
        "ip": 0,
        "op": TOTAL_FRAMES,
        "st": 0,
        "bm": 0,
        "_source_tag": source_tag,
        "_source_ind": source_ind,  # 内部标记，后续 remap 后删除
    }
    
    # 图像图层(ty=2)和预合成图层(ty=0)都需要 refId
    if el['layer_type'] in (0, 2) and refId:
        base["refId"] = refId
    
    if cl:
        base["cl"] = cl
    if parent is not None:
        base["parent"] = parent  # 后面在 ind 重新编号后再 remap
    
    if animated:
        base["ks"] = {
            "o": {"a": 1, "k": el['opacity_kfs']},
            "r": {"a": 0, "k": rot},
            "p": {"a": 1, "k": el['pos_kfs']},
            "a": {"a": 0, "k": [el['anchor'][0], el['anchor'][1], 0]},
            "s": {"a": 0, "k": el['scale']},
        }
    else:
        base["ks"] = {
            "o": {"a": 0, "k": el['opacity']},
            "r": {"a": 0, "k": rot},
            "p": {"a": 0, "k": [el['position'][0], el['position'][1], 0]},
            "a": {"a": 0, "k": [el['anchor'][0], el['anchor'][1], 0]},
            "s": {"a": 0, "k": el['scale']},
        }
    return base

# 图层排列顺序（Lottie 数组首位 = 渲染最上层）：
#   [0..n_static_top-1]  顶层静态图层（如腰封）— 永远最上层
#   [n_static_top..]     Scene B 前景 — B 盖住 A
#   [...]                Scene A 前景
#   [..., last]          底层静态图层（背景）— 永远最底层

# 静态图层按 ind 分：ind 小的在上层
static_sorted = sorted(static_layers_a, key=lambda sl: sl['ind'])

# 腰封类（ind 最小 = 顶层）vs 背景类（ind 最大 = 底层）
# 腰封 ind=1, 背景 ind=10/9
static_top = [sl for sl in static_sorted if sl['ind'] < 5]
static_bottom = [sl for sl in static_sorted if sl['ind'] >= 5]

print(f"\n=== Layer ordering ===")
print(f"  Static top ({len(static_top)}): {[s['name'] for s in static_top]}")
print(f"  Scene B FG ({len(scene_b_kfs)}): {[s['name'] for s in scene_b_kfs]}")
print(f"  Scene A FG ({len(scene_a_kfs)}): {[s['name'] for s in scene_a_kfs]}")
print(f"  Static bottom ({len(static_bottom)}): {[s['name'] for s in static_bottom]}")

# 1. 顶层静态
for sl in static_top:
    output['layers'].append(make_layer(sl, animated=False))

# 2. Scene B 前景（按原始 ind 排序）
for el in sorted(scene_b_kfs, key=lambda e: e['ind']):
    output['layers'].append(make_layer(el, animated=True))

# 3. Scene A 前景（按原始 ind 排序）
for el in sorted(scene_a_kfs, key=lambda e: e['ind']):
    output['layers'].append(make_layer(el, animated=True))

# 4. 底层静态
for sl in static_bottom:
    output['layers'].append(make_layer(sl, animated=False))

# 构建 (source_tag, source_ind) → new_ind 映射（用于 remap parent）
source_to_new = {}
for i, l in enumerate(output['layers']):
    key = (l['_source_tag'], l['_source_ind'])
    source_to_new[key] = i + 1

# 重新编号 ind 并 remap parent
for i, l in enumerate(output['layers']):
    l['ind'] = i + 1
    # Remap parent
    if 'parent' in l and l['parent'] is not None:
        old_parent = l['parent']
        parent_key = (l['_source_tag'], old_parent)
        if parent_key in source_to_new:
            l['parent'] = source_to_new[parent_key]
            print(f"  parent remap: {l['nm']} parent {old_parent} → {l['parent']}")
        else:
            print(f"  ⚠️ Parent remap failed for '{l['nm']}': parent ({l['_source_tag']}, {old_parent}) not found")
    # 清理内部标记
    del l['_source_tag']
    del l['_source_ind']

# ─── 写入文件 ──────────────────────────────────────────
with open(OUTPUT, 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False)

print(f"\n✅ 已生成: {OUTPUT}")
print(f"   总帧数: {TOTAL_FRAMES} ({TOTAL_FRAMES/FPS:.1f}s)")
print(f"   总图层: {len(output['layers'])} (静态:{len(static_top)+len(static_bottom)} + SceneA:{len(scene_a_kfs)} + SceneB:{len(scene_b_kfs)})")
print(f"   循环: frame 0 ≈ frame {TOTAL_FRAMES} (Scene A 静置)")
