"""
V7: 将两个静帧 Lottie JSON 合并为带切换动效的 Lottie JSON。

核心策略（V7 重写要点）:
1. 静态图层识别：7 维度完全匹配
   （position 相近 + anchor/scale/rotation/parent 相同 + asset 内容相同）
   不依赖图层名称（lottielab/AE 导出后 nm 常为空）
2. comp asset 完整复制，不做内容去重（嵌套层级太深，去重容易自引用）
   图片类 asset 按 base64 内容签名去重，避免同一张图存两份
3. 只做 prefix 命名空间隔离：A 的 asset id 加 "a_" 前缀，B 的加 "b_"
   comp 内部子层的 refId 用 BFS 迭代同步更新（不递归，避免爆栈）
4. 版本号、帧率、画布尺寸全部读取源文件，不硬编码
5. 时间轴基于秒定义，通过 s2f() 换算为帧数，兼容任意帧率
6. 动效参数（V6 风格）：弹性缓动 + 动态飞行距离 + overshoot/bounce

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
自查表（每次运行后核对）:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
□ 1. 版本号
    输出 v 字段是否与源文件一致？（不能是 "5.6.10" 等硬编码值）
    → 检查: print("v:", d["v"])

□ 2. 帧率与时间轴
    FPS 是否正确读取？（源文件 fr 字段，不能硬编码 30）
    总帧数 = s2f(T_TOTAL) 是否合理？（5秒@100fps=500帧，@30fps=150帧）
    → 检查脚本开头的 print 输出

□ 3. Asset 引用完整性
    所有图层的 refId 是否都能在 assets 中找到？
    → 用以下命令验证:
      python3 -c "
      import json
      d = json.load(open('merged_output.json'))
      ids = {a['id'] for a in d['assets']}
      bad = [(l.get('nm'), l.get('refId')) for l in d['layers'] if l.get('refId') and l['refId'] not in ids]
      print('Missing:', bad if bad else 'none')
      "

□ 4. comp 内部 refId
    comp 类 asset 的内部子层 refId 是否已更新为带前缀的 id？
    → 检查 prefix_asset_ids() 的 BFS 更新是否正常执行

□ 5. 静态图层识别
    脚本输出的 "Static" 列表是否合理？
    - 背景图层应该在静态列表里
    - 只有 A/B 中内容相同且位置相同的图层才应该是静态
    - 如果静态列表为空或过多，检查 is_same_asset() 逻辑

□ 6. 方向判断阈值
    get_direction() 的阈值是否基于画布比例？（W*0.3、H*0.75 等）
    不能用绝对像素值（如 400、500）

□ 7. 动效时长
    FADE（透明度过渡帧数）是否合理？
    当前 FADE=8 帧，在 100fps 下 = 0.08s，可能过短
    如需调整，改为 s2f(0.15) 这类秒制表达更安全

□ 8. JSON 合法性
    输出文件是否可被 json.loads() 正常解析？
    bejson.com / q-fe 工具能否正常识别？
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

用法:
  python generate_merged_lottie.py <文件A.json> <文件B.json> [输出目录]

示例:
  python generate_merged_lottie.py scene1.json scene2.json ./output
"""

import json, copy, sys, os

# ── 参数 ─────────────────────────────────────────────────────────────────────
if len(sys.argv) < 3:
    print("用法: python generate_merged_lottie.py <文件A.json> <文件B.json> [输出目录]")
    sys.exit(1)

FILE_A = sys.argv[1]
FILE_B = sys.argv[2]
OUTPUT_DIR = sys.argv[3] if len(sys.argv) > 3 else os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(OUTPUT_DIR, 'merged_output.json')

print(f"输入文件 A: {FILE_A}")
print(f"输入文件 B: {FILE_B}")
print(f"输出目录: {OUTPUT_DIR}\n")

# ── 读取源文件（必须先读，后续所有参数依赖源文件）────────────────────────────
with open(FILE_A, 'r', encoding='utf-8') as f: src_a = json.load(f)
with open(FILE_B, 'r', encoding='utf-8') as f: src_b = json.load(f)

# 自查点 1+2: 版本号/帧率/尺寸全部从源文件读取，禁止硬编码
FPS = max(src_a.get('fr', 30), src_b.get('fr', 30))
W   = src_a.get('w', 1125)
H   = src_a.get('h', 600)
V   = src_a.get('v', '5.7.5')   # ← 保留源文件版本号

# 自查点 2: 时间轴基于秒定义，s2f() 换算，兼容任意帧率
# V6 风格：A退场和B入场用同一窗口，center元素自然交叉溶解，无需特殊处理
def s2f(sec): return round(sec * FPS)

T_TOTAL  = 5.0   # 总时长（秒）
T_A_HOLD = 1.0   # A 静置结束 / A→B 切换窗口开始
T_SWITCH1 = 1.5  # A→B 切换窗口结束（A退场+B入场 同窗口）
T_B_HOLD = 3.5   # B 静置结束 / B→A 切换窗口开始
T_SWITCH2 = 4.0  # B→A 切换窗口结束（B退场+A入场 同窗口）

F_TOTAL     = s2f(T_TOTAL)
F_A_EXIT_S  = s2f(T_A_HOLD)    # A退场开始 = B入场开始
F_A_EXIT_E  = s2f(T_SWITCH1)   # A退场结束 = B入场结束
F_B_ENTER_S = s2f(T_A_HOLD)    # B入场开始 = A退场开始（同一窗口！）
F_B_ENTER_E = s2f(T_SWITCH1)   # B入场结束 = A退场结束
F_B_EXIT_S  = s2f(T_B_HOLD)    # B退场开始 = A入场开始
F_B_EXIT_E  = s2f(T_SWITCH2)   # B退场结束 = A入场结束
F_A_ENTER_S = s2f(T_B_HOLD)    # A入场开始 = B退场开始（同一窗口！）
F_A_ENTER_E = s2f(T_SWITCH2)   # A入场结束 = B退场结束

print(f"FPS={FPS}  v={V}  W={W}  H={H}")
print(f"TOTAL={F_TOTAL}f ({T_TOTAL}s)")
print(f"A→B 切换: {F_A_EXIT_S}→{F_A_EXIT_E} (A退场+B入场 同窗口)")
print(f"B→A 切换: {F_B_EXIT_S}→{F_B_EXIT_E} (B退场+A入场 同窗口)")

# ── Assets: 前缀命名空间隔离 ─────────────────────────────────────────────────
# 自查点 3+4: 每个来源独立加前缀，comp 内部子层 refId 用 BFS 迭代同步更新
def prefix_asset_ids(assets, prefix):
    """给所有 asset 的 id 加前缀，并同步修改 comp asset 内部子层的 refId。
    使用 BFS 迭代避免递归爆栈（深度嵌套 comp 会导致 RecursionError）。
    """
    id_map = {a['id']: prefix + a['id'] for a in assets if 'id' in a}
    new_assets = []
    for a in assets:
        na = copy.deepcopy(a)
        if 'id' in na:
            na['id'] = id_map[a['id']]
        # comp asset: BFS 更新直接子层的 refId
        if 'layers' in na:
            queue = list(na.get('layers', []))
            visited = set()
            while queue:
                layer = queue.pop(0)
                lid = id(layer)
                if lid in visited:
                    continue
                visited.add(lid)
                old_ref = layer.get('refId')
                if old_ref and old_ref in id_map:
                    layer['refId'] = id_map[old_ref]
                # 注意：不继续深入子 asset 的 layers（避免自引用死循环）
        new_assets.append(na)
    return new_assets, id_map

assets_a, id_map_a = prefix_asset_ids(src_a.get('assets', []), 'a_')
assets_b, id_map_b = prefix_asset_ids(src_b.get('assets', []), 'b_')
all_assets = assets_a + assets_b
print(f"\nAssets: A={len(assets_a)} B={len(assets_b)} total={len(all_assets)}")

# ── 提取图层元数据 ───────────────────────────────────────────────────────────
def get_pos(ks_field):
    """提取静态 position 值，返回 [x, y, z]"""
    k = ks_field.get('k', [0, 0, 0])
    if isinstance(k, list) and len(k) >= 2 and not isinstance(k[0], dict):
        return [k[0], k[1], k[2] if len(k) > 2 else 0]
    return [0, 0, 0]

def get_scalar(ks_field, default=100):
    k = ks_field.get('k', default)
    if isinstance(k, (int, float)):
        return k
    if isinstance(k, list) and len(k) > 0 and isinstance(k[0], (int, float)):
        return k[0]
    return default

def get_scale(ks_field):
    k = ks_field.get('k', [100, 100, 100])
    if isinstance(k, list) and len(k) >= 2 and not isinstance(k[0], dict):
        return [k[0], k[1], k[2] if len(k) > 2 else 100]
    return [100, 100, 100]

def extract_layer(layer, id_map):
    """从源图层提取元数据，refId 使用已加前缀的新 id"""
    ks = layer.get('ks', {})
    pos = get_pos(ks.get('p', {}))
    anc = get_pos(ks.get('a', {}))
    scl = get_scale(ks.get('s', {}))
    rot = get_scalar(ks.get('r', {}), 0)
    opa = get_scalar(ks.get('o', {}), 100)
    old_ref = layer.get('refId', '')
    new_ref = id_map.get(old_ref, old_ref) if old_ref else ''
    return {
        'ind':    layer.get('ind', 0),
        'ty':     layer.get('ty', 2),
        'nm':     layer.get('nm', ''),
        'refId':  new_ref,
        'pos':    pos,
        'anc':    anc,
        'scl':    scl,
        'rot':    rot,
        'opa':    opa,
        'parent': layer.get('parent'),
        'bm':     layer.get('bm', 0),
        'shapes': layer.get('shapes'),
        'w':      layer.get('w'),
        'h':      layer.get('h'),
        'cl':     layer.get('cl'),
        'tt':     layer.get('tt'),
        'td':     layer.get('td'),
        '_sig':   '',
    }

layers_a = [extract_layer(l, id_map_a) for l in src_a.get('layers', [])]
layers_b = [extract_layer(l, id_map_b) for l in src_b.get('layers', [])]

# 建立 asset 内容签名（图片用 base64 前 80 字符，comp 用 id）
def get_asset_sig(assets_list, ref_id):
    for a in assets_list:
        if a.get('id') == ref_id:
            if 'layers' in a:
                return ('comp', ref_id)
            return ('img', (a.get('p') or '')[:80])
    return ('?', ref_id)

for l in layers_a:
    l['_sig'] = get_asset_sig(assets_a, l['refId'])
for l in layers_b:
    l['_sig'] = get_asset_sig(assets_b, l['refId'])

# 补全 asset 尺寸（aw/ah），用于飞行距离计算
def get_asset_dims(assets_list, ref_id):
    for a in assets_list:
        if a.get('id') == ref_id:
            return a.get('w', 100), a.get('h', 100)
    return 100, 100

for l in layers_a:
    l['aw'], l['ah'] = get_asset_dims(assets_a, l['refId'])
for l in layers_b:
    l['aw'], l['ah'] = get_asset_dims(assets_b, l['refId'])

print("\n=== Layers A ===")
for l in layers_a:
    print(f"  ind={l['ind']} ty={l['ty']} nm={repr(l['nm'])} pos=[{l['pos'][0]:.0f},{l['pos'][1]:.0f}] sig={l['_sig'][0]}")
print("\n=== Layers B ===")
for l in layers_b:
    print(f"  ind={l['ind']} ty={l['ty']} nm={repr(l['nm'])} pos=[{l['pos'][0]:.0f},{l['pos'][1]:.0f}] sig={l['_sig'][0]}")

# ── Asset 去重 ────────────────────────────────────────────────────────────────
# 图片类 asset 按 base64 内容签名去重，避免同一张图存两份
_img_sig = {}
_asset_id_remap = {}
_deduped_assets = []
for a in all_assets:
    if 'layers' in a:
        _deduped_assets.append(a)
        continue
    sig = (a.get('p') or '')[:80]
    if sig in _img_sig:
        _asset_id_remap[a['id']] = _img_sig[sig]
    else:
        _img_sig[sig] = a['id']
        _deduped_assets.append(a)

if _asset_id_remap:
    n = len(_asset_id_remap)
    print(f"\nAsset 去重: {n} 个重复 image asset 已合并")
    for l in layers_a + layers_b:
        rid = l.get('refId', '')
        if rid in _asset_id_remap:
            l['refId'] = _asset_id_remap[rid]
    for a in _deduped_assets:
        if 'layers' in a:
            for sub in a['layers']:
                rid = sub.get('refId', '')
                if rid in _asset_id_remap:
                    sub['refId'] = _asset_id_remap[rid]
    all_assets = _deduped_assets
    print(f"  Assets: {len(all_assets)} (was {len(all_assets) + n})")
else:
    print("\nAsset 去重: 无重复")

# ── 识别静态图层：7 维度完全匹配 ──────────────────────────────────────────
# 两个图层必须同时满足以下所有条件才视为静态（不变元素）：
#   1. position 相近（容差 2px）   ← 调用前已检查
#   2. anchor   相近（容差 0.1）
#   3. scale    相近（容差 0.1%）
#   4. rotation 相同（容差 0.01°）
#   5. parent   相同（均为 None 或相同 int）
#   6. ty       相同（图层类型一致）
#   7. asset 内容相同（图片: base64 前80字符; 形状: 视为相同）
def pos_near(pa, pb, tol=2.0):
    return abs(pa[0] - pb[0]) < tol and abs(pa[1] - pb[1]) < tol

def transforms_same(la, lb):
    """检查两个图层的变换属性是否相同（anchor/scale/rotation/parent）"""
    # parent
    if la['parent'] != lb['parent']:
        return False
    # anchor
    if abs(la['anc'][0] - lb['anc'][0]) >= 0.1: return False
    if abs(la['anc'][1] - lb['anc'][1]) >= 0.1: return False
    # scale
    if abs(la['scl'][0] - lb['scl'][0]) >= 0.1: return False
    if abs(la['scl'][1] - lb['scl'][1]) >= 0.1: return False
    # rotation
    if abs(la['rot'] - lb['rot']) >= 0.01: return False
    return True

def is_static_pair(la, lb):
    """判断两个图层是否应被视为静态（内容+变换完全相同）"""
    if la['ty'] != lb['ty']:
        return False
    if not transforms_same(la, lb):
        return False
    # asset 内容检查
    if la['_sig'][0] == 'img' and lb['_sig'][0] == 'img':
        return la['_sig'][1] == lb['_sig'][1]
    if la['ty'] == 4 and lb['ty'] == 4:
        return True  # 形状层：变换已检查完毕
    return False  # comp 层内容通常不同

static_a, static_b = [], []
fg_a, fg_b = [], []

matched_b = set()
for la in layers_a:
    matched = False
    for i, lb in enumerate(layers_b):
        if i in matched_b:
            continue
        if pos_near(la['pos'], lb['pos']) and is_static_pair(la, lb):
            static_a.append(la)
            static_b.append(lb)
            matched_b.add(i)
            matched = True
            break
    if not matched:
        fg_a.append(la)

for i, lb in enumerate(layers_b):
    if i not in matched_b:
        fg_b.append(lb)

print(f"\n=== Static ({len(static_a)}) ===")
for l in static_a:
    print(f"  {l['nm'] or repr(l['refId'])} pos=[{l['pos'][0]:.0f},{l['pos'][1]:.0f}]")
print(f"\n=== Scene A FG ({len(fg_a)}) ===")
for l in fg_a:
    print(f"  {l['nm'] or repr(l['refId'])} pos=[{l['pos'][0]:.0f},{l['pos'][1]:.0f}]")
print(f"\n=== Scene B FG ({len(fg_b)}) ===")
for l in fg_b:
    print(f"  {l['nm'] or repr(l['refId'])} pos=[{l['pos'][0]:.0f},{l['pos'][1]:.0f}]")

# ── 动效方向 ─────────────────────────────────────────────────────────────────
# 自查点 6: 阈值基于画布比例，不用绝对像素
def get_direction(pos):
    x, y = pos[0], pos[1]
    if y > H * 0.75: return 'bottom'
    if x < W * 0.3:  return 'left'
    if x > W * 0.7:  return 'right'
    return 'center'

for l in fg_a: l['dir'] = get_direction(l['pos'])
for l in fg_b: l['dir'] = get_direction(l['pos'])

# ── 缓动曲线 ─────────────────────────────────────────────────────────────────
# 基础缓动（V6 风格）
EASE_OUT     = {"x": [0.333], "y": [0.0]}
EASE_IN      = {"x": [0.667], "y": [1.0]}
# 弹性缓动：产生 overshoot 回弹效果
EASE_SNAPPY_I = {"x": [0.667], "y": [0.667]}
EASE_SNAPPY_O = {"x": [0.333], "y": [0.333]}

def kf(t, s, ei=None, eo=None):
    if ei is None: ei = EASE_OUT
    if eo is None: eo = EASE_OUT
    v = [s] if isinstance(s, (int, float)) else list(s)
    return {"i": {"x": list(ei["x"]), "y": list(ei["y"])},
            "o": {"x": list(eo["x"]), "y": list(eo["y"])},
            "t": t, "s": v}

# ── 飞行距离 ─────────────────────────────────────────────────────────────────
def get_flight_distance(x, y, direction, aw, ah, ax, ay, sx, sy):
    """基于元素视觉边界计算飞出画布的偏移量（V6 方案）。
    visual_left  = x - ax * sx
    visual_top   = y - ay * sy
    visual_right = visual_left + aw * sx
    visual_bottom= visual_top  + ah * sy
    """
    vl = x - ax * sx
    vt = y - ay * sy
    vr = vl + aw * sx
    vb = vt + ah * sy
    margin = 80

    # 动态 overshoot 比例（小元素弹得更夸张）
    visual_area = aw * ah * sx * sy
    if visual_area < 50000:
        os_ratio = 0.10
    elif visual_area < 200000:
        os_ratio = 0.06
    else:
        os_ratio = 0.03

    if direction == "left":
        dx_in = (-margin) - vr
        return (dx_in, 0), (-dx_in * os_ratio, 0)
    elif direction == "right":
        dx_in = (W + margin) - vl
        return (dx_in, 0), (-dx_in * os_ratio, 0)
    elif direction == "bottom":
        dy_in = (H + margin) - vt
        return (0, dy_in), (0, -dy_in * os_ratio)
    elif direction == "top":
        dy_in = (-margin) - vb
        return (0, dy_in), (0, -dy_in * os_ratio)
    else:
        return (0, 0), (0, 0)

# ── 位置关键帧（含 overshoot + bounce）───────────────────────────────────────
def build_pos_kfs(l, enter_s, enter_e, exit_s, exit_e, initially_visible, stagger=0):
    x, y, z = l['pos']
    ax = l['anc'][0]; ay = l['anc'][1]
    sx = l['scl'][0] / 100.0; sy = l['scl'][1] / 100.0
    aw = l.get('aw', 100); ah = l.get('ah', 100)

    (dx_in, dy_in), (dx_os, dy_os) = get_flight_distance(
        x, y, l['dir'], aw, ah, ax, ay, sx, sy)

    entry_x   = x + dx_in
    entry_y   = y + dy_in
    overshoot_x = x + dx_os if l['dir'] != "center" else x
    overshoot_y = y + dy_os if l['dir'] != "center" else y
    es, ee = enter_s + stagger, enter_e + stagger
    xs, xe = exit_s, exit_e
    t_bounce = ee + 6
    def p3(px, py): return [px, py, z]

    kfs = []
    if initially_visible:
        kfs.append(kf(0,  p3(x, y)))
        # 退场
        kfs.append(kf(xs, p3(x, y),            EASE_IN, EASE_OUT))
        kfs.append(kf(xe, p3(entry_x, entry_y), EASE_OUT, EASE_OUT))
        # 入场（含 overshoot + bounce）
        kfs.append(kf(es, p3(entry_x, entry_y), EASE_IN, EASE_OUT))
        kfs.append(kf(ee, p3(overshoot_x, overshoot_y),
                      EASE_SNAPPY_I, EASE_SNAPPY_O))
        kfs.append(kf(t_bounce, p3(x, y),      EASE_OUT, EASE_OUT))
    else:
        kfs.append(kf(0,  p3(entry_x, entry_y), EASE_OUT, EASE_OUT))
        # 入场（含 overshoot + bounce）
        kfs.append(kf(es, p3(entry_x, entry_y), EASE_IN, EASE_OUT))
        kfs.append(kf(ee, p3(overshoot_x, overshoot_y),
                      EASE_SNAPPY_I, EASE_SNAPPY_O))
        kfs.append(kf(t_bounce, p3(x, y),      EASE_OUT, EASE_OUT))
        # 退场
        kfs.append(kf(xs, p3(x, y),            EASE_IN, EASE_OUT))
        kfs.append(kf(xe, p3(entry_x, entry_y), EASE_OUT, EASE_OUT))

    return sorted(kfs, key=lambda k: k['t'])

# ── 透明度关键帧（V6 简单逻辑，不区分 center/non-center）──────────────────────
# A退场窗口 = B入场窗口（同一窗口），center元素自然交叉溶解，无需特殊处理
FADE = 8  # 淡入淡出帧数

def build_opa_kfs(enter_s, enter_e, exit_s, exit_e, initially_visible, stagger=0):
    """opacity 与 position 窗口完全对齐，不搞交叉溶解特殊逻辑"""
    es = enter_s + stagger
    ee = enter_e + stagger
    xs = exit_s
    xe = exit_e
    if initially_visible:
        # A: 可见 → 退场淡出 → 不可见 → 入场淡入 → 可见
        return sorted([
            kf(0,   [100]),
            kf(xs,  [100], EASE_IN,  EASE_IN),
            kf(xe,  [0],   EASE_IN,  EASE_IN),
            kf(es,  [0]),
            kf(ee,  [100], EASE_OUT, EASE_OUT),
        ], key=lambda k: k['t'])
    else:
        # B: 不可见 → 入场淡入 → 可见 → 退场淡出 → 不可见
        return sorted([
            kf(0,   [0]),
            kf(es,  [0]),
            kf(ee,  [100], EASE_OUT, EASE_OUT),
            kf(xs,  [100], EASE_IN,  EASE_IN),
            kf(xe,  [0],   EASE_IN,  EASE_IN),
        ], key=lambda k: k['t'])

# ── 构建图层 JSON ─────────────────────────────────────────────────────────────
out_layers = []
orig_ind_to_new = {}  # (tag, orig_ind) -> new_ind，用于 parent remap

def _layer_base(l, tag):
    """构建图层公共字段"""
    layer = {
        "ddd": 0,
        "ty":  l['ty'],
        "nm":  l['nm'],
        "sr": 1, "ao": 0, "bm": l.get('bm', 0),
        "ip": 0, "op": F_TOTAL, "st": 0,
        "_tag": tag, "_orig_ind": l['ind'],
    }
    if l['refId']:                          layer['refId'] = l['refId']
    if l.get('cl'):                         layer['cl'] = l['cl']
    if l.get('tt') is not None:             layer['tt'] = l['tt']
    if l.get('td') is not None:             layer['td'] = l['td']
    if l['ty'] == 4 and l.get('shapes'):   layer['shapes'] = l['shapes']
    if l['ty'] == 0:
        if l.get('w'): layer['w'] = l['w']
        if l.get('h'): layer['h'] = l['h']
    if l.get('parent') is not None:        layer['parent'] = l['parent']
    return layer

def make_static_layer(l, tag):
    layer = _layer_base(l, tag)
    layer["ks"] = {
        "o": {"a": 0, "k": l['opa']},
        "r": {"a": 0, "k": l['rot']},
        "p": {"a": 0, "k": l['pos']},
        "a": {"a": 0, "k": l['anc']},
        "s": {"a": 0, "k": l['scl']},
    }
    return layer

def make_anim_layer(l, tag, enter_s, enter_e, exit_s, exit_e, initially_visible, stagger=0):
    """V6 简单逻辑：不区分 center/non-center"""
    pos_kfs = build_pos_kfs(l, enter_s, enter_e, exit_s, exit_e, initially_visible, stagger)
    opa_kfs = build_opa_kfs(enter_s, enter_e, exit_s, exit_e, initially_visible, stagger)
    layer = _layer_base(l, tag)
    layer["ks"] = {
        "o": {"a": 1, "k": opa_kfs},
        "r": {"a": 0, "k": l['rot']},
        "p": {"a": 1, "k": pos_kfs},
        "a": {"a": 0, "k": l['anc']},
        "s": {"a": 0, "k": l['scl']},
    }
    return layer

# 图层顺序（Lottie 数组首位 = 渲染最上层）:
#   静态顶层（ind < 5，如腰封）→ B 前景 → A 前景 → 静态底层（如背景）
# 注意：ind 阈值 5 是基于源文件排列的经验值，如需调整请修改此处
static_sorted = sorted(static_a, key=lambda l: l['ind'])
static_top    = [l for l in static_sorted if l['ind'] < 5]
static_bot    = [l for l in static_sorted if l['ind'] >= 5]

for l in static_top:
    out_layers.append(make_static_layer(l, 'a'))

def calc_stagger(i, l, H):
    """错帧：索引 + 垂直位置（高处元素先入场）"""
    y = l['pos'][1]
    bonus = max(0, int((H - y) / 180))
    return i * 3 + bonus

for i, l in enumerate(sorted(fg_b, key=lambda l: l['ind'])):
    out_layers.append(make_anim_layer(l, 'b',
        F_B_ENTER_S, F_B_ENTER_E, F_B_EXIT_S, F_B_EXIT_E,
        initially_visible=False, stagger=calc_stagger(i, l, H)))

for i, l in enumerate(sorted(fg_a, key=lambda l: l['ind'])):
    out_layers.append(make_anim_layer(l, 'a',
        F_A_ENTER_S, F_A_ENTER_E, F_A_EXIT_S, F_A_EXIT_E,
        initially_visible=True, stagger=calc_stagger(i, l, H)))

for l in static_bot:
    out_layers.append(make_static_layer(l, 'a'))

# 编号 + parent remap
for i, l in enumerate(out_layers):
    l['ind'] = i + 1
    orig_ind_to_new[(l['_tag'], l['_orig_ind'])] = i + 1

for l in out_layers:
    if l.get('parent') is not None:
        key = (l['_tag'], l['parent'])
        if key in orig_ind_to_new:
            l['parent'] = orig_ind_to_new[key]
        else:
            del l['parent']  # 父级未找到时删除，避免错误引用
    del l['_tag']
    del l['_orig_ind']

print(f"\n=== Output layers ({len(out_layers)}) ===")
for l in out_layers:
    p_anim = l['ks']['p'].get('a', 0)
    o_anim = l['ks']['o'].get('a', 0)
    print(f"  ind={l['ind']} ty={l['ty']} nm={repr(l['nm'])} ref={l.get('refId','-')} p_anim={p_anim} o_anim={o_anim}")

# ── 自查点 3: 引用完整性验证 ──────────────────────────────────────────────────
asset_ids = {a['id'] for a in all_assets}
bad_refs = [(l.get('nm'), l.get('refId')) for l in out_layers if l.get('refId') and l['refId'] not in asset_ids]
if bad_refs:
    print(f"\n⚠️  Missing refIds: {bad_refs}")
else:
    print("\n✅ All refIds valid")

# ── 自查点 9: 循环衔接一致性验证（首帧=尾帧） ───────────────────────────────
# 循环播放时，t=0 的状态必须与 t=OP(总帧) 完全一致，否则会产生跳变/闪烁
# 关键修复：即使数据上首尾值相等也必须显式补入 t=OP 关键帧
# 因为 lottie 从 OP 跳回 0 时如果没有明确的 OP 锚点，可能产生插值异常
from copy import deepcopy as _dc

loop_fixed = 0
for l in out_layers:
    for prop in ['o', 'p']:
        ks = l['ks'].get(prop, {})
        if ks.get('a') != 1:
            continue
        kfs = ks['k']
        if not kfs:
            continue
        v_start = kfs[0]['s']  # t=0 的值（这就是循环回到开头时的状态）
        
        # 无条件在 t=F_TOTAL 处补入一个与 t=0 完全相同的关键帧
        # 这样 lottie 在 OP 处有明确锚点，确保无缝循环
        new_kf = _dc(kfs[0])
        new_kf['t'] = F_TOTAL
        new_kf['s'] = list(v_start) if isinstance(v_start, list) else v_start
        # 清除缓动信息（循环点不需要缓动）
        new_kf.pop('i', None)
        new_kf.pop('o', None)
        kfs.append(new_kf)
        kfs.sort(key=lambda k: k['t'])
        loop_fixed += 1

print(f"✅ 循环衔接: 为所有动画属性在 t={F_TOTAL} 处补入首帧锚点 (共 {loop_fixed} 处)")

# ── 输出 ──────────────────────────────────────────────────────────────────────
output = {
    "v":    V,        # 自查点 1: 源文件版本号
    "fr":   FPS,      # 自查点 2: 源文件帧率
    "ip":   0,
    "op":   F_TOTAL,
    "w":    W,
    "h":    H,
    "nm":   "Merged",
    "ddd":  0,
    "assets": all_assets,
    "layers": out_layers,
}

os.makedirs(OUTPUT_DIR, exist_ok=True)
with open(OUTPUT, 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False)

# ── 预览 HTML 生成（单一模板，杜绝 fetch/embedded 逻辑漂移）──────────────────────
# 历史 bug：fetch 版和 embedded 版曾各自维护一份 HTML，导致 FileSaver.js 下载修复
#   只存在于 fetch 版，embedded 版丢了 saveAs() 逻辑，下载按钮跳转网页。
# 修复方案：抽出 build_preview_html() 单一函数，两种模式共用同一份 CSS+JS，
#   仅 jsonData 赋值方式和 bootstrap 流程不同。改一处即同步两版。
PREVIEW = os.path.join(OUTPUT_DIR, 'preview.html')
PREVIEW_EMBEDDED = os.path.join(OUTPUT_DIR, 'preview_embedded.html')
json_str = json.dumps(output, ensure_ascii=False, separators=(',', ':'))

def build_preview_html(mode, json_str=None):
    """生成预览 HTML。
    mode='fetch'    — jsonData 从 fetch('merged_output.json') 加载，需 HTTP 服务器
    mode='embedded' — jsonData 直接内嵌，双击打开即可
    两种模式共享同一份 CSS + JS（含 FileSaver.js saveAs 下载修复），仅数据加载方式不同。
    """
    assert mode in ('fetch', 'embedded'), f'unknown mode: {mode}'
    if mode == 'embedded':
        assert json_str is not None, 'embedded 模式需要 json_str'
        title = 'Lottie 切换动效预览（内嵌模式）'
        json_line = f'var jsonData = {json_str};'
        # embedded: 数据已就绪，直接加载 lottie-web → initAnimation → 预加载 FileSaver
        bootstrap = """st.textContent = 'Lottie库加载中...';
loadCdn(function(err) {
  if (err) { st.style.color = '#f88'; st.textContent = '❌ Lottie库加载失败: ' + err.message; return; }
  if (typeof lottie === 'undefined') { st.style.color = '#f88'; st.textContent = '❌ Lottie对象未定义'; return; }
  initAnimation();
  loadFileSaver(function() {});
});"""
    else:
        title = 'Lottie 切换动效预览'
        json_line = 'var jsonData = null;'
        # fetch: 先 fetch JSON → 再加载 lottie-web → initAnimation → 预加载 FileSaver
        bootstrap = """var ts = new Date().getTime();
st.textContent = '正在加载动画数据...';
fetch('merged_output.json?t=' + ts)
  .then(function(r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  })
  .then(function(d) {
    jsonData = d;
    st.textContent = 'Lottie库加载中...';
    loadCdn(function(err) {
      if (err) {
        st.style.color = '#f88';
        st.textContent = '❌ Lottie库加载失败: ' + err.message + ' (请检查网络或刷新重试)';
        return;
      }
      if (typeof lottie === 'undefined') {
        st.style.color = '#f88';
        st.textContent = '❌ Lottie对象未定义';
        return;
      }
      initAnimation();
      loadFileSaver(function() {});
    });
  })
  .catch(function(err) {
    st.style.color = '#f88';
    st.textContent = '❌ 数据加载失败: ' + err.message + ' (需通过 HTTP 服务器打开，不能直接双击)';
  });"""

    # 单一模板：CSS + HTML body + 共享 JS 函数，只有 {{TITLE}} / {{JSON_LINE}} / {{BOOTSTRAP}} 三个插值点
    return '''<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>__TITLE__</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#1a1a2e;display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;font-family:-apple-system,sans-serif;color:#eee}
h2{margin-bottom:16px;font-weight:400;color:#aaa;font-size:16px}
#lc{width:562px;height:300px;background:#222;border-radius:8px;overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,.4)}
.ctl{margin-top:20px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;justify-content:center}
button{padding:8px 20px;border:1px solid #555;border-radius:6px;background:#2a2a4a;color:#eee;cursor:pointer;font-size:14px}
button:hover{background:#3a3a6a}
.sp button{background:#222}.sp button.active{background:#5a5aff;border-color:#5a5aff}
#dl-btn{background:#1a4a2a;border-color:#2a7a4a;color:#7ef5a0}
#dl-btn:hover{background:#1e5a32}
#fi,#st{font-size:13px;margin-top:10px;min-height:20px}
</style></head>
<body>
<h2>__TITLE__</h2>
<div id="lc"></div>
<div class="ctl">
  <button onclick="doToggle()" id="btnToggle">&#9208; 暂停</button>
  <button onclick="doReplay()">&#8634; 重播</button>
  <div class="sp">
    <button onclick="ss(0.5)" id="s05">0.5x</button>
    <button onclick="ss(1)" id="s10" class="active">1x</button>
    <button onclick="ss(2)" id="s20">2x</button>
  </div>
  <button id="dl-btn" onclick="dlJson()">&#11015; 下载 JSON</button>
</div>
<div id="fi"></div>
<div id="st" style="color:#ff8">加载中...</div>
<script>
// 全局变量（onclick 必须能访问到，禁止用 IIFE 包裹）
var st = document.getElementById('st');
var fi = document.getElementById('fi');
var anim = null;
__JSON_LINE__

var CDN_URLS = [
  "https://cdn.jsdelivr.net/npm/lottie-web@5.12.2/build/player/lottie.min.js",
  "https://cdnjs.cloudflare.com/ajax/libs/lottie-web/5.12.2/lottie.min.js"
];
var FSAVER_URLS = [
  "https://cdn.jsdelivr.net/npm/file-saver@2.0.5/dist/FileSaver.min.js",
  "https://cdnjs.cloudflare.com/ajax/libs/FileSaver.js/2.0.5/FileSaver.min.js"
];
var cdnIdx = 0;

function doToggle() {
  if (!anim) return;
  var btn = document.getElementById('btnToggle');
  if (anim.isPaused) { anim.play(); btn.innerHTML = '&#9208; 暂停'; }
  else { anim.pause(); btn.innerHTML = '&#9654; 播放'; }
}
function doReplay() {
  if (anim) { anim.goToAndPlay(0, true); document.getElementById('btnToggle').innerHTML = '&#9208; 暂停'; }
}
function ss(s) {
  if (anim) anim.setSpeed(s);
  document.querySelectorAll('.sp button').forEach(function(b) { b.classList.remove('active'); });
  var btnId = 's' + (s + '').replace('.', '0');
  var btn = document.getElementById(btnId);
  if (btn) btn.classList.add('active');
}
function dlJson() {
  if (!jsonData) { alert('JSON 尚未加载完成'); return; }
  var data = JSON.stringify(jsonData, null, 2);
  // 用 FileSaver.js saveAs() 确保文件名正确（原生 a.download 对 blob URL 可能失效，导致跳转网页）
  var blob = new Blob([data], {type: 'application/json;charset=utf-8'});
  if (typeof saveAs === 'function') {
    saveAs(blob, 'merged_output.json');
  } else {
    // FileSaver 未加载时的降级方案
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = 'merged_output.json';
    document.body.appendChild(a); a.click();
    setTimeout(function() { if (a.parentNode) document.body.removeChild(a); URL.revokeObjectURL(url); }, 2000);
  }
}

function loadCdn(cb) {
  var s = document.createElement('script');
  s.src = CDN_URLS[cdnIdx];
  s.onload = function() { cb(null); };
  s.onerror = function() {
    cdnIdx++;
    if (cdnIdx < CDN_URLS.length) { loadCdn(cb); } else { cb(new Error('所有CDN均失败')); }
  };
  document.head.appendChild(s);
}
function loadFileSaver(cb) {
  var fsIdx = 0;
  function tryNext() {
    var s = document.createElement('script');
    s.src = FSAVER_URLS[fsIdx];
    s.onload = function() { cb(null); };
    s.onerror = function() {
      fsIdx++;
      if (fsIdx < FSAVER_URLS.length) { tryNext(); } else { cb(new Error('FileSaver CDN 失败（降级使用原生下载）')); }
    };
    document.head.appendChild(s);
  }
  tryNext();
}

function initAnimation() {
  try {
    anim = lottie.loadAnimation({
      container: document.getElementById('lc'), renderer: 'svg', loop: true, autoplay: true, animationData: jsonData
    });
    anim.addEventListener('enterFrame', function() { fi.textContent = '帧: ' + Math.round(anim.currentFrame) + ' / ' + anim.totalFrames; });
    anim.addEventListener('data_ready', function() { st.style.color = '#8f8'; st.textContent = '✅ 加载完成，正在播放...'; });
    anim.addEventListener('data_failed', function() { st.style.color = '#f88'; st.textContent = '❌ 数据解析失败'; });
    anim.addEventListener('error', function(e) { st.style.color = '#f88'; st.textContent = '渲染错误: ' + (e.error ? e.error.message : JSON.stringify(e)); });
  } catch(e) { st.style.color = '#f88'; st.textContent = '初始化失败: ' + e.message; }
}

__BOOTSTRAP__
</script>
</body></html>'''.replace('__TITLE__', title).replace('__JSON_LINE__', json_line).replace('__BOOTSTRAP__', bootstrap)

# 生成 fetch 版（需 HTTP 服务器）
html_fetch = build_preview_html('fetch')
with open(PREVIEW, 'w', encoding='utf-8') as f:
    f.write(html_fetch)

# 生成内嵌版（双击打开，无需服务器）
html_embedded = build_preview_html('embedded', json_str)
with open(PREVIEW_EMBEDDED, 'w', encoding='utf-8') as f:
    f.write(html_embedded)

# ── 预览 HTML 自检（防止 FileSaver 逻辑再次丢失）──────────────────────────────
for label, path in [('fetch', PREVIEW), ('embedded', PREVIEW_EMBEDDED)]:
    with open(path, encoding='utf-8') as f:
        content = f.read()
    checks = {
        'saveAs调用': 'saveAs(blob' in content,
        'FileSaver CDN': 'file-saver' in content,
        'loadFileSaver函数': 'function loadFileSaver' in content,
        '降级方案': 'URL.createObjectURL' in content,
    }
    failed = [k for k, v in checks.items() if not v]
    if failed:
        print(f'❌ 预览自检失败 [{label}]: 缺少 {failed}')
        sys.exit(1)
    else:
        print(f'✅ 预览自检 [{label}]: saveAs + FileSaver + 降级方案 齐全')

print(f"\n✅ 输出: {OUTPUT}")
print(f"   v={V}  fr={FPS}  op={F_TOTAL}  w={W}  h={H}")
print(f"   layers={len(out_layers)}  assets={len(all_assets)}")
print(f"✅ 预览(fetch): {PREVIEW}  (需启动 http.server)")
print(f"✅ 预览(内嵌): {PREVIEW_EMBEDDED}  (双击打开，无需服务器)")
print(f"\n━━━ 自查提示 ━━━")
print(f"1. 用 bejson.com/ui/lottie 验证 JSON 可解析")
print(f"2. 检查静态图层数量是否合理（预期包含背景图层）")
print(f"3. 检查 FADE={FADE} 帧在 {FPS}fps 下 = {FADE/FPS:.2f}s，是否过短")
