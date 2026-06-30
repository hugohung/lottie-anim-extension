"""
Lottie 静帧合并动效 — 分阶段流水线版（V8.1-pipeline）

架构：6 阶段解耦流水线，每阶段独立函数 + 中间产物 + 自检 + 局部重跑
  Stage 0 Parse     — 读 JSON · 规范化变换属性 · asset 去重
  Stage 1 Classify  — 7 维度匹配 · 静态/前景分组 · 方向判断
  Stage 2 Timeline  — in/hold/out 区间 · 错峰 · 时间轴分配
  Stage 3 Keyframes — 位移 · 透明度关键帧生成
  Stage 4 Assemble  — 图层排序 · ind 编号 · 循环锚点 · refId 验证
  Stage 5 Preview   — 单一模板 fetch/embedded · 自检

用法:
  # 全跑
  python generate_merged_lottie_pipeline.py a.json b.json output/
  # 从 Stage 2 重跑到末尾（复用 0/1 的中间产物）
  python generate_merged_lottie_pipeline.py a.json b.json output/ --from 2
  # 只重跑 Stage 3 到 Stage 5
  python generate_merged_lottie_pipeline.py a.json b.json output/ --from 3 --to 5

中间产物: output/pipeline/00_parse.json, 01_classify.json, 02_timeline.json, 03_keyframes.json
"""

import json, copy, sys, os, argparse
from copy import deepcopy

# ═══════════════════════════════════════════════════════════════════════════════
# 工具函数（所有阶段共用）
# ═══════════════════════════════════════════════════════════════════════════════

def _to_jsonable(obj):
    """递归把 tuple 转 list，让中间产物可 JSON 序列化"""
    if isinstance(obj, tuple):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, list):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj

def _write_json(path, data):
    """写中间产物 JSON（pretty print，方便人工检查）"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(_to_jsonable(data), f, ensure_ascii=False, indent=2)

def _read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)

def _fail(stage, msg):
    """自检失败：打印错误并退出"""
    print(f"\n❌ [Stage {stage} 自检失败] {msg}")
    sys.exit(1)

def _ok(stage, msg):
    print(f"✅ [Stage {stage}] {msg}")


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 0: Parse — 读取输入文件，规范化图层属性，asset 去重
# ═══════════════════════════════════════════════════════════════════════════════

def _get_pos(ks_field):
    k = ks_field.get('k', [0, 0, 0])
    if isinstance(k, list) and len(k) >= 2 and not isinstance(k[0], dict):
        return [k[0], k[1], k[2] if len(k) > 2 else 0]
    return [0, 0, 0]

def _get_scalar(ks_field, default=100):
    k = ks_field.get('k', default)
    if isinstance(k, (int, float)):
        return k
    if isinstance(k, list) and len(k) > 0 and isinstance(k[0], (int, float)):
        return k[0]
    return default

def _get_scale(ks_field):
    k = ks_field.get('k', [100, 100, 100])
    if isinstance(k, list) and len(k) >= 2 and not isinstance(k[0], dict):
        return [k[0], k[1], k[2] if len(k) > 2 else 100]
    return [100, 100, 100]

def _prefix_asset_ids(assets, prefix):
    """给所有 asset 的 id 加前缀，comp 内部子层 refId 用 BFS 同步更新"""
    id_map = {a['id']: prefix + a['id'] for a in assets if 'id' in a}
    new_assets = []
    for a in assets:
        na = copy.deepcopy(a)
        if 'id' in na:
            na['id'] = id_map[a['id']]
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
        new_assets.append(na)
    return new_assets, id_map

def _extract_layer(layer, id_map):
    """从源图层提取规范化元数据"""
    ks = layer.get('ks', {})
    old_ref = layer.get('refId', '')
    new_ref = id_map.get(old_ref, old_ref) if old_ref else ''
    return {
        'ind':    layer.get('ind', 0),
        'ty':     layer.get('ty', 2),
        'nm':     layer.get('nm', ''),
        'refId':  new_ref,
        'pos':    _get_pos(ks.get('p', {})),
        'anc':    _get_pos(ks.get('a', {})),
        'scl':    _get_scale(ks.get('s', {})),
        'rot':    _get_scalar(ks.get('r', {}), 0),
        'opa':    _get_scalar(ks.get('o', {}), 100),
        'parent': layer.get('parent'),
        'bm':     layer.get('bm', 0),
        'shapes': layer.get('shapes'),
        'w':      layer.get('w'),
        'h':      layer.get('h'),
        'cl':     layer.get('cl'),
        'tt':     layer.get('tt'),
        'td':     layer.get('td'),
    }

def _get_asset_sig(assets_list, ref_id):
    for a in assets_list:
        if a.get('id') == ref_id:
            if 'layers' in a:
                return ['comp', ref_id]
            return ['img', (a.get('p') or '')[:80]]
    return ['?', ref_id]

def _get_asset_dims(assets_list, ref_id):
    for a in assets_list:
        if a.get('id') == ref_id:
            return a.get('w', 100), a.get('h', 100)
    return 100, 100

def stage_parse(file_a, file_b):
    """Stage 0: 读取两个源 JSON，规范化图层，asset 去重"""
    with open(file_a, encoding='utf-8') as f: src_a = json.load(f)
    with open(file_b, encoding='utf-8') as f: src_b = json.load(f)

    FPS = max(src_a.get('fr', 30), src_b.get('fr', 30))
    W   = src_a.get('w', 1125)
    H   = src_a.get('h', 600)
    V   = src_a.get('v', '5.7.5')

    # asset 前缀隔离
    assets_a, id_map_a = _prefix_asset_ids(src_a.get('assets', []), 'a_')
    assets_b, id_map_b = _prefix_asset_ids(src_b.get('assets', []), 'b_')
    all_assets = assets_a + assets_b

    # 提取图层
    layers_a = [_extract_layer(l, id_map_a) for l in src_a.get('layers', [])]
    layers_b = [_extract_layer(l, id_map_b) for l in src_b.get('layers', [])]

    # 签名 + 尺寸
    for l in layers_a:
        l['_sig'] = _get_asset_sig(assets_a, l['refId'])
        l['aw'], l['ah'] = _get_asset_dims(assets_a, l['refId'])
    for l in layers_b:
        l['_sig'] = _get_asset_sig(assets_b, l['refId'])
        l['aw'], l['ah'] = _get_asset_dims(assets_b, l['refId'])

    # 图片 asset 去重
    _img_sig = {}
    _asset_id_remap = {}
    _deduped = []
    for a in all_assets:
        if 'layers' in a:
            _deduped.append(a)
            continue
        sig = (a.get('p') or '')[:80]
        if sig in _img_sig:
            _asset_id_remap[a['id']] = _img_sig[sig]
        else:
            _img_sig[sig] = a['id']
            _deduped.append(a)
    if _asset_id_remap:
        for l in layers_a + layers_b:
            rid = l.get('refId', '')
            if rid in _asset_id_remap:
                l['refId'] = _asset_id_remap[rid]
        for a in _deduped:
            if 'layers' in a:
                for sub in a['layers']:
                    rid = sub.get('refId', '')
                    if rid in _asset_id_remap:
                        sub['refId'] = _asset_id_remap[rid]
        all_assets = _deduped

    return {
        'meta': {'v': V, 'fr': FPS, 'w': W, 'h': H},
        'assets': all_assets,
        'layers_a': layers_a,
        'layers_b': layers_b,
        'dedup_count': len(_asset_id_remap),
    }

def stage_parse_check(d):
    """Stage 0 自检"""
    m = d['meta']
    assert m['fr'] > 0, 'fr 必须 > 0'
    assert m['w'] > 0 and m['h'] > 0, 'w/h 必须 > 0'
    assert d['layers_a'], 'layers_a 为空'
    assert d['layers_b'], 'layers_b 为空'
    # refId 完整性
    asset_ids = {a['id'] for a in d['assets']}
    for label, layers in [('A', d['layers_a']), ('B', d['layers_b'])]:
        for l in layers:
            if l.get('refId') and l['refId'] not in asset_ids:
                _fail(0, f'{label} 图层 {l.get("nm")} refId={l["refId"]} 不在 assets 中')
    _ok(0, f'解析完成 A={len(d["layers_a"])}层 B={len(d["layers_b"])}层 assets={len(d["assets"])} 去重={d["dedup_count"]}')


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 1: Classify — 7 维度静态匹配 + 前景分组 + 方向判断
# ═══════════════════════════════════════════════════════════════════════════════

def _pos_near(pa, pb, tol=2.0):
    return abs(pa[0] - pb[0]) < tol and abs(pa[1] - pb[1]) < tol

def _transforms_same(la, lb):
    if la['parent'] != lb['parent']: return False
    if abs(la['anc'][0] - lb['anc'][0]) >= 0.1: return False
    if abs(la['anc'][1] - lb['anc'][1]) >= 0.1: return False
    if abs(la['scl'][0] - lb['scl'][0]) >= 0.1: return False
    if abs(la['scl'][1] - lb['scl'][1]) >= 0.1: return False
    if abs(la['rot'] - lb['rot']) >= 0.01: return False
    return True

def _is_static_pair(la, lb):
    if la['ty'] != lb['ty']: return False
    if not _transforms_same(la, lb): return False
    if la['_sig'][0] == 'img' and lb['_sig'][0] == 'img':
        return la['_sig'][1] == lb['_sig'][1]
    if la['ty'] == 4 and lb['ty'] == 4:
        return True
    return False

def _get_direction(pos, W, H):
    x, y = pos[0], pos[1]
    if y > H * 0.75: return 'bottom'
    if x < W * 0.3:  return 'left'
    if x > W * 0.7:  return 'right'
    return 'center'

def stage_classify(parse_out):
    """Stage 1: 静态识别 + 前景分组 + 方向"""
    W = parse_out['meta']['w']
    H = parse_out['meta']['h']
    layers_a = parse_out['layers_a']
    layers_b = parse_out['layers_b']

    static_pairs = []
    matched_b = set()
    fg_a = []
    for la in layers_a:
        matched = False
        for i, lb in enumerate(layers_b):
            if i in matched_b: continue
            if _pos_near(la['pos'], lb['pos']) and _is_static_pair(la, lb):
                static_pairs.append({'a': la, 'b': lb})
                matched_b.add(i)
                matched = True
                break
        if not matched:
            fg_a.append(la)
    fg_b = [lb for i, lb in enumerate(layers_b) if i not in matched_b]

    for l in fg_a: l['dir'] = _get_direction(l['pos'], W, H)
    for l in fg_b: l['dir'] = _get_direction(l['pos'], W, H)

    return {
        'meta': parse_out['meta'],
        'assets': parse_out['assets'],
        'static_pairs': static_pairs,
        'fg_a': fg_a,
        'fg_b': fg_b,
    }

def stage_classify_check(d):
    """Stage 1 自检"""
    # 静态配对不能有重复
    a_inds = [p['a']['ind'] for p in d['static_pairs']]
    b_inds = [p['b']['ind'] for p in d['static_pairs']]
    assert len(a_inds) == len(set(a_inds)), 'static_pairs 中 A 侧有重复 ind'
    assert len(b_inds) == len(set(b_inds)), 'static_pairs 中 B 侧有重复 ind'
    # 数量守恒：static_a + fg_a = layers_a
    # (这里不重新算 layers_a，只检查合理性)
    assert d['fg_a'] or d['fg_b'], 'fg_a 和 fg_b 都为空，输入可能有问题'
    _ok(1, f'静态={len(d["static_pairs"])}对 A前景={len(d["fg_a"])} B前景={len(d["fg_b"])}')


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 2: Timeline — 时间轴参数 + 每元素时间分配
# ═══════════════════════════════════════════════════════════════════════════════

def _s2f(sec, fps): return round(sec * fps)

def _calc_stagger(i, l, H):
    y = l['pos'][1]
    bonus = max(0, int((H - y) / 180))
    return i * 3 + bonus

def stage_timeline(classify_out, params=None):
    """Stage 2: 为每个前景元素分配 enter/exit 时间窗口 + stagger"""
    meta = classify_out['meta']
    fps = meta['fr']
    H = meta['h']

    # 默认时间轴参数（V8 风格，可被 params 覆盖）
    p = {
        'T_TOTAL': 5.0,
        'T_A_HOLD': 1.0,
        'T_SWITCH1': 1.5,
        'T_B_HOLD': 3.5,
        'T_SWITCH2': 4.0,
    }
    if params:
        p.update(params)

    F_TOTAL     = _s2f(p['T_TOTAL'], fps)
    F_A_EXIT_S  = _s2f(p['T_A_HOLD'], fps)
    F_A_EXIT_E  = _s2f(p['T_SWITCH1'], fps)
    F_B_EXIT_S  = _s2f(p['T_B_HOLD'], fps)
    F_B_EXIT_E  = _s2f(p['T_SWITCH2'], fps)
    # A入场 = B退场窗口；B入场 = A退场窗口（同一窗口）

    static_pairs = classify_out['static_pairs']
    static_sorted = sorted([p['a'] for p in static_pairs], key=lambda l: l['ind'])
    static_top = [l for l in static_sorted if l['ind'] < 5]
    static_bot = [l for l in static_sorted if l['ind'] >= 5]

    b_timeline = []
    for i, l in enumerate(sorted(classify_out['fg_b'], key=lambda l: l['ind'])):
        b_timeline.append({
            'layer': l,
            'enter_s': F_A_EXIT_S,  # B入场 = A退场窗口开始（同一窗口）
            'enter_e': F_A_EXIT_E,
            'exit_s':  F_B_EXIT_S,
            'exit_e':  F_B_EXIT_E,
            'initially_visible': False,
            'stagger': _calc_stagger(i, l, H),
        })

    a_timeline = []
    for i, l in enumerate(sorted(classify_out['fg_a'], key=lambda l: l['ind'])):
        a_timeline.append({
            'layer': l,
            'enter_s': F_B_EXIT_S,  # A入场 = B退场窗口开始
            'enter_e': F_B_EXIT_E,
            'exit_s':  F_A_EXIT_S,
            'exit_e':  F_A_EXIT_E,
            'initially_visible': True,
            'stagger': _calc_stagger(i, l, H),
        })

    return {
        'meta': meta,
        'assets': classify_out['assets'],
        'timeline_params': {
            'F_TOTAL': F_TOTAL, 'F_A_EXIT_S': F_A_EXIT_S, 'F_A_EXIT_E': F_A_EXIT_E,
            'F_B_EXIT_S': F_B_EXIT_S, 'F_B_EXIT_E': F_B_EXIT_E,
        },
        'static_top': static_top,
        'static_bot': static_bot,
        'b_timeline': b_timeline,
        'a_timeline': a_timeline,
    }

def stage_timeline_check(d):
    """Stage 2 自检"""
    tp = d['timeline_params']
    F = tp['F_TOTAL']
    # 时间戳递增
    assert tp['F_A_EXIT_S'] < tp['F_A_EXIT_E'], 'A退场窗口起点必须 < 终点'
    assert tp['F_B_EXIT_S'] < tp['F_B_EXIT_E'], 'B退场窗口起点必须 < 终点'
    assert tp['F_A_EXIT_E'] <= tp['F_B_EXIT_S'], 'A退场结束必须 <= B退场开始'
    assert tp['F_B_EXIT_E'] <= F, 'B退场结束必须 <= F_TOTAL'
    # 每个元素的 stagger 后时间不能超过 F_TOTAL
    for label, tl in [('A', d['a_timeline']), ('B', d['b_timeline'])]:
        for item in tl:
            s = item['stagger']
            assert item['exit_e'] + s <= F, f'{label} 元素 stagger={s} 导致 exit_e+s={item["exit_e"]+s} > F_TOTAL={F}'
    _ok(2, f'时间轴 F_TOTAL={F} A切换={tp["F_A_EXIT_S"]}→{tp["F_A_EXIT_E"]} B切换={tp["F_B_EXIT_S"]}→{tp["F_B_EXIT_E"]}')


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 3: Keyframes — 位移 + 透明度关键帧生成
# ═══════════════════════════════════════════════════════════════════════════════

EASE_OUT      = {"x": [0.333], "y": [0.0]}
EASE_IN       = {"x": [0.667], "y": [1.0]}
EASE_SNAPPY_I = {"x": [0.667], "y": [0.667]}
EASE_SNAPPY_O = {"x": [0.333], "y": [0.333]}
FADE = 8  # 淡入淡出帧数

def _kf(t, s, ei=None, eo=None):
    if ei is None: ei = EASE_OUT
    if eo is None: eo = EASE_OUT
    v = [s] if isinstance(s, (int, float)) else list(s)
    return {"i": {"x": list(ei["x"]), "y": list(ei["y"])},
            "o": {"x": list(eo["x"]), "y": list(eo["y"])},
            "t": t, "s": v}

def _get_flight_distance(x, y, direction, aw, ah, ax, ay, sx, sy, W, H):
    vl = x - ax * sx
    vt = y - ay * sy
    vr = vl + aw * sx
    vb = vt + ah * sy
    margin = 80
    visual_area = aw * ah * sx * sy
    if visual_area < 50000:   os_ratio = 0.10
    elif visual_area < 200000: os_ratio = 0.06
    else:                      os_ratio = 0.03

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

def _build_pos_kfs(l, enter_s, enter_e, exit_s, exit_e, initially_visible, stagger, W, H):
    x, y, z = l['pos']
    ax, ay = l['anc'][0], l['anc'][1]
    sx, sy = l['scl'][0] / 100.0, l['scl'][1] / 100.0
    aw, ah = l.get('aw', 100), l.get('ah', 100)
    (dx_in, dy_in), (dx_os, dy_os) = _get_flight_distance(
        x, y, l['dir'], aw, ah, ax, ay, sx, sy, W, H)
    entry_x = x + dx_in
    entry_y = y + dy_in
    overshoot_x = x + dx_os if l['dir'] != "center" else x
    overshoot_y = y + dy_os if l['dir'] != "center" else y
    es, ee = enter_s + stagger, enter_e + stagger
    xs, xe = exit_s, exit_e
    t_bounce = ee + 6
    def p3(px, py): return [px, py, z]
    kfs = []
    if initially_visible:
        kfs.append(_kf(0,  p3(x, y)))
        kfs.append(_kf(xs, p3(x, y),            EASE_IN, EASE_OUT))
        kfs.append(_kf(xe, p3(entry_x, entry_y), EASE_OUT, EASE_OUT))
        kfs.append(_kf(es, p3(entry_x, entry_y), EASE_IN, EASE_OUT))
        kfs.append(_kf(ee, p3(overshoot_x, overshoot_y), EASE_SNAPPY_I, EASE_SNAPPY_O))
        kfs.append(_kf(t_bounce, p3(x, y),      EASE_OUT, EASE_OUT))
    else:
        kfs.append(_kf(0,  p3(entry_x, entry_y), EASE_OUT, EASE_OUT))
        kfs.append(_kf(es, p3(entry_x, entry_y), EASE_IN, EASE_OUT))
        kfs.append(_kf(ee, p3(overshoot_x, overshoot_y), EASE_SNAPPY_I, EASE_SNAPPY_O))
        kfs.append(_kf(t_bounce, p3(x, y),      EASE_OUT, EASE_OUT))
        kfs.append(_kf(xs, p3(x, y),            EASE_IN, EASE_OUT))
        kfs.append(_kf(xe, p3(entry_x, entry_y), EASE_OUT, EASE_OUT))
    return sorted(kfs, key=lambda k: k['t'])

def _build_opa_kfs(enter_s, enter_e, exit_s, exit_e, initially_visible, stagger):
    es = enter_s + stagger
    ee = enter_e + stagger
    xs, xe = exit_s, exit_e
    if initially_visible:
        return sorted([
            _kf(0,   [100]),
            _kf(xs,  [100], EASE_IN,  EASE_IN),
            _kf(xe,  [0],   EASE_IN,  EASE_IN),
            _kf(es,  [0]),
            _kf(ee,  [100], EASE_OUT, EASE_OUT),
        ], key=lambda k: k['t'])
    else:
        return sorted([
            _kf(0,   [0]),
            _kf(es,  [0]),
            _kf(ee,  [100], EASE_OUT, EASE_OUT),
            _kf(xs,  [100], EASE_IN,  EASE_IN),
            _kf(xe,  [0],   EASE_IN,  EASE_IN),
        ], key=lambda k: k['t'])

def _layer_base(l, tag, F_TOTAL):
    layer = {
        "ddd": 0, "ty": l['ty'], "nm": l['nm'], "sr": 1, "ao": 0,
        "bm": l.get('bm', 0), "ip": 0, "op": F_TOTAL, "st": 0,
        "_tag": tag, "_orig_ind": l['ind'],
    }
    if l['refId']:                      layer['refId'] = l['refId']
    if l.get('cl'):                     layer['cl'] = l['cl']
    if l.get('tt') is not None:         layer['tt'] = l['tt']
    if l.get('td') is not None:         layer['td'] = l['td']
    if l['ty'] == 4 and l.get('shapes'): layer['shapes'] = l['shapes']
    if l['ty'] == 0:
        if l.get('w'): layer['w'] = l['w']
        if l.get('h'): layer['h'] = l['h']
    if l.get('parent') is not None:     layer['parent'] = l['parent']
    return layer

def _make_static_layer(l, tag, F_TOTAL):
    layer = _layer_base(l, tag, F_TOTAL)
    layer["ks"] = {
        "o": {"a": 0, "k": l['opa']},
        "r": {"a": 0, "k": l['rot']},
        "p": {"a": 0, "k": l['pos']},
        "a": {"a": 0, "k": l['anc']},
        "s": {"a": 0, "k": l['scl']},
    }
    return layer

def _make_anim_layer(l, tag, enter_s, enter_e, exit_s, exit_e, initially_visible, stagger, W, H, F_TOTAL):
    pos_kfs = _build_pos_kfs(l, enter_s, enter_e, exit_s, exit_e, initially_visible, stagger, W, H)
    opa_kfs = _build_opa_kfs(enter_s, enter_e, exit_s, exit_e, initially_visible, stagger)
    layer = _layer_base(l, tag, F_TOTAL)
    layer["ks"] = {
        "o": {"a": 1, "k": opa_kfs},
        "r": {"a": 0, "k": l['rot']},
        "p": {"a": 1, "k": pos_kfs},
        "a": {"a": 0, "k": l['anc']},
        "s": {"a": 0, "k": l['scl']},
    }
    return layer

def stage_keyframes(timeline_out):
    """Stage 3: 生成所有图层的 ks（变换属性）"""
    meta = timeline_out['meta']
    W, H = meta['w'], meta['h']
    F_TOTAL = timeline_out['timeline_params']['F_TOTAL']
    out_layers = []

    # 静态顶层
    for l in timeline_out['static_top']:
        out_layers.append(_make_static_layer(l, 'a', F_TOTAL))
    # B 前景
    for item in timeline_out['b_timeline']:
        l = item['layer']
        out_layers.append(_make_anim_layer(l, 'b',
            item['enter_s'], item['enter_e'], item['exit_s'], item['exit_e'],
            item['initially_visible'], item['stagger'], W, H, F_TOTAL))
    # A 前景
    for item in timeline_out['a_timeline']:
        l = item['layer']
        out_layers.append(_make_anim_layer(l, 'a',
            item['enter_s'], item['enter_e'], item['exit_s'], item['exit_e'],
            item['initially_visible'], item['stagger'], W, H, F_TOTAL))
    # 静态底层
    for l in timeline_out['static_bot']:
        out_layers.append(_make_static_layer(l, 'a', F_TOTAL))

    return {
        'meta': meta,
        'assets': timeline_out['assets'],
        'timeline_params': timeline_out['timeline_params'],
        'layers': out_layers,
    }

def stage_keyframes_check(d):
    """Stage 3 自检：关键帧时间戳递增 + 维度正确 + opacity 策略"""
    issues = []
    for i, l in enumerate(d['layers']):
        nm = l.get('nm', f'layer_{i}')
        ks = l.get('ks', {})
        for prop in ['o', 'p']:
            pk = ks.get(prop, {})
            if pk.get('a') != 1: continue
            kfs = pk.get('k', [])
            ts = [k['t'] for k in kfs]
            # 时间戳严格递增
            for j in range(1, len(ts)):
                if ts[j] <= ts[j-1]:
                    issues.append(f'{nm}.{prop}[{j}] t={ts[j-1]} >= t={ts[j]}')
            # t=0 不能重复
            if ts.count(0) > 1:
                issues.append(f'{nm}.{prop} t=0 出现 {ts.count(0)} 次')
            # position 必须 3 维
            if prop == 'p':
                for ki, kf in enumerate(kfs):
                    s = kf.get('s')
                    if not isinstance(s, list) or len(s) < 3:
                        issues.append(f'{nm}.p[{ki}]@t={kf["t"]} s 维度={len(s) if isinstance(s,list) else "非list"}')
            # s 不能为 None
            for ki, kf in enumerate(kfs):
                if kf.get('s') is None:
                    issues.append(f'{nm}.{prop}[{ki}]@t={kf["t"]} s=None')
    if issues:
        _fail(3, f'{len(issues)} 个关键帧问题: {issues[:5]}...')
    _ok(3, f'关键帧生成 {len(d["layers"])} 层，时间戳+维度全部合法')


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 4: Assemble — ind 编号 + parent remap + 循环锚点 + refId 验证
# ═══════════════════════════════════════════════════════════════════════════════

def stage_assemble(keyframes_out):
    """Stage 4: 组装最终 Lottie JSON"""
    meta = keyframes_out['meta']
    F_TOTAL = keyframes_out['timeline_params']['F_TOTAL']
    out_layers = deepcopy(keyframes_out['layers'])
    assets = keyframes_out['assets']

    # ind 编号 + parent remap
    orig_ind_to_new = {}
    for i, l in enumerate(out_layers):
        l['ind'] = i + 1
        orig_ind_to_new[(l['_tag'], l['_orig_ind'])] = i + 1
    for l in out_layers:
        if l.get('parent') is not None:
            key = (l['_tag'], l['parent'])
            if key in orig_ind_to_new:
                l['parent'] = orig_ind_to_new[key]
            else:
                del l['parent']
        del l['_tag']
        del l['_orig_ind']

    # 循环锚点：t=F_TOTAL 处补入首帧
    loop_fixed = 0
    for l in out_layers:
        for prop in ['o', 'p']:
            ks = l['ks'].get(prop, {})
            if ks.get('a') != 1: continue
            kfs = ks['k']
            if not kfs: continue
            v_start = kfs[0]['s']
            new_kf = deepcopy(kfs[0])
            new_kf['t'] = F_TOTAL
            new_kf['s'] = list(v_start) if isinstance(v_start, list) else v_start
            new_kf.pop('i', None)
            new_kf.pop('o', None)
            kfs.append(new_kf)
            kfs.sort(key=lambda k: k['t'])
            loop_fixed += 1

    output = {
        "v": meta['v'], "fr": meta['fr'], "ip": 0, "op": F_TOTAL,
        "w": meta['w'], "h": meta['h'], "nm": "Merged", "ddd": 0,
        "assets": assets, "layers": out_layers,
    }
    return output, loop_fixed

def stage_assemble_check(output, loop_fixed):
    """Stage 4 自检：refId 完整性 + 循环锚点"""
    asset_ids = {a['id'] for a in output['assets']}
    bad = [(l.get('nm'), l.get('refId')) for l in output['layers']
           if l.get('refId') and l['refId'] not in asset_ids]
    if bad:
        _fail(4, f'refId 引用缺失: {bad}')
    assert loop_fixed > 0, '循环锚点数为 0，可能没有动画层'
    _ok(4, f'组装完成 {len(output["layers"])}层 {len(output["assets"])}assets 循环锚点{loop_fixed}处')


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 5: Preview — 单一模板生成 fetch/embedded 预览
# ═══════════════════════════════════════════════════════════════════════════════

def _build_preview_html(mode, json_str=None):
    """单一模板：fetch/embedded 共用，仅 jsonData 赋值方式不同"""
    assert mode in ('fetch', 'embedded'), f'unknown mode: {mode}'
    if mode == 'embedded':
        assert json_str is not None, 'embedded 模式需要 json_str'
        title = 'Lottie 切换动效预览（内嵌模式）'
        json_line = f'var jsonData = {json_str};'
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
      if (err) { st.style.color = '#f88'; st.textContent = '❌ Lottie库加载失败: ' + err.message + ' (请检查网络或刷新重试)'; return; }
      if (typeof lottie === 'undefined') { st.style.color = '#f88'; st.textContent = '❌ Lottie对象未定义'; return; }
      initAnimation();
      loadFileSaver(function() {});
    });
  })
  .catch(function(err) { st.style.color = '#f88'; st.textContent = '❌ 数据加载失败: ' + err.message + ' (需通过 HTTP 服务器打开)'; });"""

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
var st = document.getElementById('st');
var fi = document.getElementById('fi');
var anim = null;
__JSON_LINE__

var CDN_URLS = ["https://cdn.jsdelivr.net/npm/lottie-web@5.12.2/build/player/lottie.min.js","https://cdnjs.cloudflare.com/ajax/libs/lottie-web/5.12.2/lottie.min.js"];
var FSAVER_URLS = ["https://cdn.jsdelivr.net/npm/file-saver@2.0.5/dist/FileSaver.min.js","https://cdnjs.cloudflare.com/ajax/libs/FileSaver.js/2.0.5/FileSaver.min.js"];
var cdnIdx = 0;

function doToggle() { if (!anim) return; var btn = document.getElementById('btnToggle'); if (anim.isPaused) { anim.play(); btn.innerHTML = '&#9208; 暂停'; } else { anim.pause(); btn.innerHTML = '&#9654; 播放'; } }
function doReplay() { if (anim) { anim.goToAndPlay(0, true); document.getElementById('btnToggle').innerHTML = '&#9208; 暂停'; } }
function ss(s) { if (anim) anim.setSpeed(s); document.querySelectorAll('.sp button').forEach(function(b) { b.classList.remove('active'); }); var btnId = 's' + (s + '').replace('.', '0'); var btn = document.getElementById(btnId); if (btn) btn.classList.add('active'); }
function dlJson() {
  if (!jsonData) { alert('JSON 尚未加载完成'); return; }
  var data = JSON.stringify(jsonData, null, 2);
  var blob = new Blob([data], {type: 'application/json;charset=utf-8'});
  if (typeof saveAs === 'function') { saveAs(blob, 'merged_output.json'); }
  else { var url = URL.createObjectURL(blob); var a = document.createElement('a'); a.href = url; a.download = 'merged_output.json'; document.body.appendChild(a); a.click(); setTimeout(function() { if (a.parentNode) document.body.removeChild(a); URL.revokeObjectURL(url); }, 2000); }
}
function loadCdn(cb) { var s = document.createElement('script'); s.src = CDN_URLS[cdnIdx]; s.onload = function() { cb(null); }; s.onerror = function() { cdnIdx++; if (cdnIdx < CDN_URLS.length) { loadCdn(cb); } else { cb(new Error('所有CDN均失败')); } }; document.head.appendChild(s); }
function loadFileSaver(cb) { var fsIdx = 0; function tryNext() { var s = document.createElement('script'); s.src = FSAVER_URLS[fsIdx]; s.onload = function() { cb(null); }; s.onerror = function() { fsIdx++; if (fsIdx < FSAVER_URLS.length) { tryNext(); } else { cb(new Error('FileSaver CDN 失败（降级使用原生下载）')); } }; document.head.appendChild(s); } tryNext(); }
function initAnimation() {
  try {
    anim = lottie.loadAnimation({ container: document.getElementById('lc'), renderer: 'svg', loop: true, autoplay: true, animationData: jsonData });
    anim.addEventListener('enterFrame', function() { fi.textContent = '帧: ' + Math.round(anim.currentFrame) + ' / ' + anim.totalFrames; });
    anim.addEventListener('data_ready', function() { st.style.color = '#8f8'; st.textContent = '✅ 加载完成，正在播放...'; });
    anim.addEventListener('data_failed', function() { st.style.color = '#f88'; st.textContent = '❌ 数据解析失败'; });
    anim.addEventListener('error', function(e) { st.style.color = '#f88'; st.textContent = '渲染错误: ' + (e.error ? e.error.message : JSON.stringify(e)); });
  } catch(e) { st.style.color = '#f88'; st.textContent = '初始化失败: ' + e.message; }
}

__BOOTSTRAP__
</script>
</body></html>'''.replace('__TITLE__', title).replace('__JSON_LINE__', json_line).replace('__BOOTSTRAP__', bootstrap)

def stage_preview(output, output_dir):
    """Stage 5: 生成 fetch + embedded 预览"""
    preview_fetch = os.path.join(output_dir, 'preview.html')
    preview_embedded = os.path.join(output_dir, 'preview_embedded.html')
    json_str = json.dumps(output, ensure_ascii=False, separators=(',', ':'))

    with open(preview_fetch, 'w', encoding='utf-8') as f:
        f.write(_build_preview_html('fetch'))
    with open(preview_embedded, 'w', encoding='utf-8') as f:
        f.write(_build_preview_html('embedded', json_str))

    return preview_fetch, preview_embedded

def stage_preview_check(preview_fetch, preview_embedded):
    """Stage 5 自检：FileSaver 逻辑齐全"""
    for label, path in [('fetch', preview_fetch), ('embedded', preview_embedded)]:
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
            _fail(5, f'[{label}] 缺少 {failed}')
    _ok(5, f'预览生成 fetch + embedded，FileSaver 逻辑齐全')


# ═══════════════════════════════════════════════════════════════════════════════
# 主流程：流水线调度 + 局部重跑
# ═══════════════════════════════════════════════════════════════════════════════

PIPELINE_DIR = 'pipeline'
STAGE_FILES = {
    0: 'pipeline/00_parse.json',
    1: 'pipeline/01_classify.json',
    2: 'pipeline/02_timeline.json',
    3: 'pipeline/03_keyframes.json',
}

def run_pipeline(file_a, file_b, output_dir, from_stage=0, to_stage=5, timeline_params=None):
    """运行流水线，支持局部重跑"""
    os.makedirs(output_dir, exist_ok=True)
    pipe_dir = os.path.join(output_dir, PIPELINE_DIR)
    os.makedirs(pipe_dir, exist_ok=True)

    print(f"━━━ 流水线启动 [Stage {from_stage} → {to_stage}] ━━━\n")

    # ── Stage 0 ──
    if from_stage <= 0 <= to_stage:
        parse_out = stage_parse(file_a, file_b)
        stage_parse_check(parse_out)
        _write_json(os.path.join(output_dir, STAGE_FILES[0]), parse_out)
    else:
        parse_out = _read_json(os.path.join(output_dir, STAGE_FILES[0]))
        print(f"⏭️  [Stage 0] 复用 {STAGE_FILES[0]}")

    # ── Stage 1 ──
    if from_stage <= 1 <= to_stage:
        classify_out = stage_classify(parse_out)
        stage_classify_check(classify_out)
        _write_json(os.path.join(output_dir, STAGE_FILES[1]), classify_out)
    else:
        classify_out = _read_json(os.path.join(output_dir, STAGE_FILES[1]))
        print(f"⏭️  [Stage 1] 复用 {STAGE_FILES[1]}")

    # ── Stage 2 ──
    if from_stage <= 2 <= to_stage:
        timeline_out = stage_timeline(classify_out, timeline_params)
        stage_timeline_check(timeline_out)
        _write_json(os.path.join(output_dir, STAGE_FILES[2]), timeline_out)
    else:
        timeline_out = _read_json(os.path.join(output_dir, STAGE_FILES[2]))
        print(f"⏭️  [Stage 2] 复用 {STAGE_FILES[2]}")

    # ── Stage 3 ──
    if from_stage <= 3 <= to_stage:
        keyframes_out = stage_keyframes(timeline_out)
        stage_keyframes_check(keyframes_out)
        _write_json(os.path.join(output_dir, STAGE_FILES[3]), keyframes_out)
    else:
        keyframes_out = _read_json(os.path.join(output_dir, STAGE_FILES[3]))
        print(f"⏭️  [Stage 3] 复用 {STAGE_FILES[3]}")

    # ── Stage 4 ──
    if from_stage <= 4 <= to_stage:
        output, loop_fixed = stage_assemble(keyframes_out)
        stage_assemble_check(output, loop_fixed)
        output_path = os.path.join(output_dir, 'merged_output.json')
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False)
        print(f"✅ 输出: {output_path}")
    else:
        output_path = os.path.join(output_dir, 'merged_output.json')
        output = _read_json(output_path)
        print(f"⏭️  [Stage 4] 复用 {output_path}")

    # ── Stage 5 ──
    if from_stage <= 5 <= to_stage:
        preview_fetch, preview_embedded = stage_preview(output, output_dir)
        stage_preview_check(preview_fetch, preview_embedded)
        print(f"✅ 预览(fetch): {preview_fetch}")
        print(f"✅ 预览(内嵌): {preview_embedded}  (双击打开)")

    print(f"\n━━━ 流水线完成 ━━━")


def main():
    parser = argparse.ArgumentParser(description='Lottie 静帧合并动效 — 分阶段流水线')
    parser.add_argument('file_a', nargs='?', help='场景 A JSON（全跑模式必需，局部重跑可省略）')
    parser.add_argument('file_b', nargs='?', help='场景 B JSON（全跑模式必需，局部重跑可省略）')
    parser.add_argument('output_dir', nargs='?', default='./output', help='输出目录')
    parser.add_argument('--from', dest='from_stage', type=int, default=0, help='从哪个阶段开始 (0-5)')
    parser.add_argument('--to', dest='to_stage', type=int, default=5, help='到哪个阶段结束 (0-5)')
    args = parser.parse_args()

    # 局部重跑模式：from_stage > 0 时，第一个位置参数是 output_dir
    if args.from_stage > 0:
        # 重新解释位置参数：file_a 实际是 output_dir
        if args.file_a:
            args.output_dir = args.file_a
            args.file_a = None
        args.file_b = None
    else:
        if not args.file_a or not args.file_b:
            parser.error('全跑模式需要 file_a 和 file_b 参数\n  用法: python generate_merged_lottie_pipeline.py a.json b.json output/')

    run_pipeline(args.file_a, args.file_b, args.output_dir,
                 args.from_stage, args.to_stage)

if __name__ == '__main__':
    main()
