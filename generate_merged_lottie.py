"""
V7: 将两个静帧 Lottie JSON 合并为带切换动效的 Lottie JSON。

核心策略（V7 重写要点）:
1. 不靠图层名称识别静态图层（lottielab/AE等工具导出后 nm 字段常为空）
   改用 position 坐标相近 + asset base64 内容相同 双重匹配
2. comp asset 完整复制，不做内容去重（嵌套层级太深，去重容易自引用）
3. 只做 prefix 命名空间隔离：A 的 asset id 加 "a_" 前缀，B 的加 "b_"
   comp 内部子层的 refId 用 BFS 迭代同步更新（不递归，避免爆栈）
4. 版本号、帧率、画布尺寸全部读取源文件，不硬编码
5. 时间轴基于秒定义，通过 s2f() 换算为帧数，兼容任意帧率

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
def s2f(sec): return round(sec * FPS)

T_TOTAL  = 5.0   # 总时长（秒）
T_A_END  = 0.2   # A 静置结束
T_AB_MID = 0.5   # A→B 切换中点
T_B_END  = 4.0   # B 静置结束
T_BA_MID = 4.3   # B→A 切换中点

F_TOTAL     = s2f(T_TOTAL)
F_A_EXIT_S  = s2f(T_A_END)
F_A_EXIT_E  = s2f(T_AB_MID)
F_B_ENTER_S = s2f((T_A_END + T_AB_MID) / 2)
F_B_ENTER_E = s2f(T_AB_MID + 0.15)
F_B_EXIT_S  = s2f(T_B_END)
F_B_EXIT_E  = s2f(T_BA_MID)
F_A_ENTER_S = s2f((T_B_END + T_BA_MID) / 2)
F_A_ENTER_E = s2f(T_BA_MID + 0.15)

print(f"FPS={FPS}  v={V}  W={W}  H={H}")
print(f"TOTAL={F_TOTAL}f ({T_TOTAL}s)")
print(f"A exit:  {F_A_EXIT_S}→{F_A_EXIT_E}")
print(f"B enter: {F_B_ENTER_S}→{F_B_ENTER_E}")
print(f"B exit:  {F_B_EXIT_S}→{F_B_EXIT_E}")
print(f"A enter: {F_A_ENTER_S}→{F_A_ENTER_E}")

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

print("\n=== Layers A ===")
for l in layers_a:
    print(f"  ind={l['ind']} ty={l['ty']} nm={repr(l['nm'])} pos=[{l['pos'][0]:.0f},{l['pos'][1]:.0f}] sig={l['_sig'][0]}")
print("\n=== Layers B ===")
for l in layers_b:
    print(f"  ind={l['ind']} ty={l['ty']} nm={repr(l['nm'])} pos=[{l['pos'][0]:.0f},{l['pos'][1]:.0f}] sig={l['_sig'][0]}")

# ── 识别静态图层：position 相近 AND asset 内容相同 ───────────────────────────
# 自查点 5: 静态图层识别策略
# 注意：不依赖图层名（nm），因为 lottielab/AE 导出后 nm 常为空字符串
# 如果图层名有意义（设计师用 "bg:" "static:" 等前缀命名），可在此处增加名字匹配作为加分项
def pos_near(pa, pb, tol=2.0):
    return abs(pa[0] - pb[0]) < tol and abs(pa[1] - pb[1]) < tol

def is_same_asset(la, lb):
    """判断两个图层是否为同一内容（用于识别静态图层）:
    - 图片类(ty=2): base64 前 80 字符相同
    - 形状类(ty=4): 位置相同即认为静态
    - comp类(ty=0): 通常 A/B 文件的 comp 内容不同，不认为相同
    """
    if la['ty'] == 4 and lb['ty'] == 4:
        return pos_near(la['pos'], lb['pos'])
    if la['_sig'][0] == 'img' and lb['_sig'][0] == 'img':
        return la['_sig'][1] == lb['_sig'][1]
    return False

static_a, static_b = [], []
fg_a, fg_b = [], []

matched_b = set()
for la in layers_a:
    matched = False
    for i, lb in enumerate(layers_b):
        if i in matched_b:
            continue
        if pos_near(la['pos'], lb['pos']) and is_same_asset(la, lb):
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
EI_OUT = {"x": [0.167], "y": [0.167]}
EO_OUT = {"x": [0.833], "y": [0.833]}
EI_IN  = {"x": [0.667], "y": [1.0]}
EO_IN  = {"x": [0.333], "y": [0.0]}

def kf(t, s, ei=None, eo=None):
    if ei is None: ei = EI_OUT
    if eo is None: eo = EO_OUT
    v = [s] if isinstance(s, (int, float)) else list(s)
    return {"i": {"x": list(ei["x"]), "y": list(ei["y"])},
            "o": {"x": list(eo["x"]), "y": list(eo["y"])},
            "t": t, "s": v}

# ── 飞入/飞出偏移量 ──────────────────────────────────────────────────────────
def fly_offset(direction, dist=500):
    """返回画面外的偏移量 (dx, dy)，元素从这个位置飞入"""
    if direction == 'left':   return (-dist, 0)
    if direction == 'right':  return ( dist, 0)
    if direction == 'bottom': return (0,  dist)
    if direction == 'top':    return (0, -dist)
    return (0, 0)  # center: 纯淡入淡出

# ── 位置关键帧 ───────────────────────────────────────────────────────────────
def build_pos_kfs(l, enter_s, enter_e, exit_s, exit_e, initially_visible, stagger=0):
    x, y, z = l['pos']
    dx, dy = fly_offset(l['dir'])
    off_x, off_y = x + dx, y + dy
    es, ee = enter_s + stagger, enter_e + stagger
    xs, xe = exit_s, exit_e
    def p3(px, py): return [px, py, z]

    if l['dir'] == 'center':
        return None  # center 方向只做透明度，位置不动

    if initially_visible:
        return sorted([
            kf(0,  p3(x, y)),
            kf(xs, p3(x, y),        EI_IN, EO_IN),
            kf(xe, p3(off_x, off_y)),
            kf(es, p3(off_x, off_y)),
            kf(ee, p3(x, y),        EI_IN, EO_IN),
        ], key=lambda k: k['t'])
    else:
        return sorted([
            kf(0,  p3(off_x, off_y)),
            kf(es, p3(off_x, off_y)),
            kf(ee, p3(x, y),        EI_IN, EO_IN),
            kf(xs, p3(x, y),        EI_IN, EO_IN),
            kf(xe, p3(off_x, off_y)),
        ], key=lambda k: k['t'])

# ── 透明度关键帧 ─────────────────────────────────────────────────────────────
# 自查点 7: FADE 帧数影响视觉效果，可改为 s2f(0.15) 更安全
FADE = 8  # 淡入淡出帧数（100fps 下 = 0.08s，30fps 下 = 0.27s）

def build_opa_kfs(enter_s, enter_e, exit_s, exit_e, initially_visible, stagger=0):
    es = enter_s + stagger
    xs = exit_s
    if initially_visible:
        return sorted([
            kf(0,        [100]),
            kf(xs,       [100], EI_IN, EO_IN),
            kf(xs+FADE,  [0]),
            kf(es,       [0]),
            kf(es+FADE,  [100]),
        ], key=lambda k: k['t'])
    else:
        return sorted([
            kf(0,        [0]),
            kf(es,       [0]),
            kf(es+FADE,  [100]),
            kf(xs,       [100], EI_IN, EO_IN),
            kf(xs+FADE,  [0]),
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
    pos_kfs = build_pos_kfs(l, enter_s, enter_e, exit_s, exit_e, initially_visible, stagger)
    opa_kfs = build_opa_kfs(enter_s, enter_e, exit_s, exit_e, initially_visible, stagger)
    layer = _layer_base(l, tag)
    layer["ks"] = {
        "o": {"a": 1, "k": opa_kfs},
        "r": {"a": 0, "k": l['rot']},
        "p": {"a": 1, "k": pos_kfs} if pos_kfs is not None else {"a": 0, "k": l['pos']},
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

for i, l in enumerate(sorted(fg_b, key=lambda l: l['ind'])):
    out_layers.append(make_anim_layer(l, 'b',
        F_B_ENTER_S, F_B_ENTER_E, F_B_EXIT_S, F_B_EXIT_E,
        initially_visible=False, stagger=i*3))

for i, l in enumerate(sorted(fg_a, key=lambda l: l['ind'])):
    out_layers.append(make_anim_layer(l, 'a',
        F_A_ENTER_S, F_A_ENTER_E, F_A_EXIT_S, F_A_EXIT_E,
        initially_visible=True, stagger=i*3))

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

# ── 生成预览 HTML（含下载按钮）────────────────────────────────────────────────
PREVIEW = os.path.join(OUTPUT_DIR, 'preview.html')
with open(OUTPUT, 'r', encoding='utf-8') as f:
    json_str = f.read()

html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>Lottie 切换动效预览</title>
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
<h2>Lottie 切换动效预览</h2>
<div id="lc"></div>
<div class="ctl">
  <button onclick="anim&&anim.play()">&#9654; 播放</button>
  <button onclick="anim&&anim.pause()">&#9208; 暂停</button>
  <button onclick="anim&&anim.goToAndPlay(0,true)">&#8634; 重播</button>
  <div class="sp">
    <button onclick="ss(0.5)" id="s05">0.5x</button>
    <button onclick="ss(1)" id="s10" class="active">1x</button>
    <button onclick="ss(2)" id="s20">2x</button>
  </div>
  <button id="dl-btn" onclick="dlJson()">&#11015; 下载 JSON</button>
</div>
<div id="fi"></div>
<div id="st" style="color:#ff8">加载中...</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/lottie-web/5.12.2/lottie.min.js"></script>
<script>
var d=''' + json_str + ''';
var st=document.getElementById('st'),fi=document.getElementById('fi'),anim=null;
function ss(s){if(anim)anim.setSpeed(s);document.querySelectorAll('.sp button').forEach(function(b){b.classList.remove('active')});document.getElementById('s'+(s+'').replace('.','0')).classList.add('active')}
function dlJson(){
  var blob=new Blob([JSON.stringify(d)],{type:'application/json'});
  var a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download='merged_output.json';
  a.click();
  URL.revokeObjectURL(a.href);
}
function init(){
  if(typeof lottie==='undefined'){st.style.color='#f88';st.textContent='Lottie库加载失败，请检查网络';return}
  try{
    anim=lottie.loadAnimation({container:document.getElementById('lc'),renderer:'svg',loop:true,autoplay:true,animationData:d});
    anim.addEventListener('enterFrame',function(){fi.textContent='帧: '+Math.round(anim.currentFrame)+' / '+anim.totalFrames});
    anim.addEventListener('data_ready',function(){st.style.color='#8f8';st.textContent='✅ 加载完成，正在播放...'});
    anim.addEventListener('data_failed',function(){st.style.color='#f88';st.textContent='❌ 数据解析失败'});
    anim.addEventListener('error',function(e){st.style.color='#f88';st.textContent='渲染错误: '+JSON.stringify(e)});
  }catch(e){st.style.color='#f88';st.textContent='初始化失败: '+e.message}
}
if(typeof lottie!=='undefined')init();
else{var c=0,t=setInterval(function(){if(typeof lottie!=='undefined'){clearInterval(t);init()}else if(++c>80){clearInterval(t);st.style.color='#f88';st.textContent='Lottie加载超时，请刷新'}},100)}
</script>
</body></html>'''

with open(PREVIEW, 'w', encoding='utf-8') as f:
    f.write(html)

print(f"\n✅ 输出: {OUTPUT}")
print(f"   v={V}  fr={FPS}  op={F_TOTAL}  w={W}  h={H}")
print(f"   layers={len(out_layers)}  assets={len(all_assets)}")
print(f"✅ 预览: {PREVIEW}")
print(f"\n━━━ 自查提示 ━━━")
print(f"1. 用 bejson.com/ui/lottie 验证 JSON 可解析")
print(f"2. 检查静态图层数量是否合理（预期包含背景图层）")
print(f"3. 检查 FADE={FADE} 帧在 {FPS}fps 下 = {FADE/FPS:.2f}s，是否过短")
