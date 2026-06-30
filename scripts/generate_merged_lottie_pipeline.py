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
    """判断两个图层是否为静态配对（用户规则：大小+位置一致 = 静态）。
    不要求图像字节级一致——lottielab 重新导出会使同一张图 base64 不同，
    故改用 asset 尺寸(aw/ah)判断"大小一样"。"""
    if la['ty'] != lb['ty']: return False
    if not _transforms_same(la, lb): return False
    # 图像层：比较 asset 尺寸（aw/ah），不比较 base64
    if la['ty'] == 2 and lb['ty'] == 2:
        return la.get('aw', 0) == lb.get('aw', 0) and la.get('ah', 0) == lb.get('ah', 0)
    # 形状层：变换一致即静态
    if la['ty'] == 4 and lb['ty'] == 4:
        return True
    return False

def _get_direction(pos, W, H):
    """单个元素方向判断（被组方向调用）"""
    x, y = pos[0], pos[1]
    if y > H * 0.75: return 'bottom'
    if x < W * 0.3:  return 'left'
    if x > W * 0.7:  return 'right'
    return 'center'

# ── L1 纯代码分组逻辑 ──────────────────────────────────────────────────────────
# 三条规则按优先级执行：
#   1. 图层名语义匹配 → 同类整组（如所有"气球"归一组）
#   2. 空间聚类 → 距离相近的 product 归一组
#   3. 兜底 → 没匹配上的各自独立

# 语义关键词字典（不写死类型，按关键词匹配）
_SEMANTIC_KEYWORDS = {
    'text':       ['补贴', '领千元', '感叹号', 'title', '标题', '文案', '价格', '优惠', '满减', '折扣'],
    'decoration': ['气球', '星星', '五角星', 'joy', 'star', 'balloon', '装饰', '彩带', '烟花', '光效'],
}

def _classify_semantic(nm):
    """根据图层名判断语义类型，返回类型名或 None"""
    nm_lower = nm.lower()
    for sem_type, keywords in _SEMANTIC_KEYWORDS.items():
        if any(k in nm_lower for k in keywords):
            return sem_type
    return None

def _spatial_cluster(layers, W, threshold_ratio=0.25):
    """空间聚类：距离相近的图层归为一组
    threshold = W * threshold_ratio（默认画布宽度的 25%）
    返回 list of groups，每个 group 是 layer index 列表
    """
    if not layers:
        return []
    threshold = W * threshold_ratio
    groups = []  # 每个 group 是 {'center': [cx, cy], 'indices': [i, ...]}
    for i, l in enumerate(layers):
        x, y = l['pos'][0], l['pos'][1]
        # 找最近的已有组
        best_g = None
        best_dist = float('inf')
        for g in groups:
            cx, cy = g['center']
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best_g = g
        if best_g and best_dist < threshold:
            # 加入已有组，更新中心
            best_g['indices'].append(i)
            n = len(best_g['indices'])
            best_g['center'][0] = (best_g['center'][0] * (n - 1) + x) / n
            best_g['center'][1] = (best_g['center'][1] * (n - 1) + y) / n
        else:
            # 新组
            groups.append({'center': [x, y], 'indices': [i]})
    return [g['indices'] for g in groups]

def _load_group_config(output_dir):
    """加载 group_config.json（部分覆盖），返回 overrides 列表或 None"""
    config_path = os.path.join(output_dir, 'group_config.json')
    if not os.path.exists(config_path):
        return None
    with open(config_path, encoding='utf-8') as f:
        cfg = json.load(f)
    return cfg.get('overrides', [])

def _apply_overrides(layers, overrides):
    """应用人工微调覆盖：把指定图层归为一组
    overrides: [{"layers": ["榴莲.png", "可乐.png"], "dir": "right"}, ...]
    返回 (group_assignments, forced_dirs) 
      group_assignments: {layer_nm: group_id}
      forced_dirs: {group_id: direction}
    """
    assignments = {}
    forced_dirs = {}
    for ov in overrides:
        gid = f"override_{len(forced_dirs)}"
        for nm in ov.get('layers', []):
            assignments[nm] = gid
        if 'dir' in ov:
            forced_dirs[gid] = ov['dir']
    return assignments, forced_dirs

def _auto_group(fg_layers, W, H):
    """L1 纯代码自动分组 — 直接在 fg_layers 每个图层上设置 group_id 和 dir。
    不再用 nm 做 dict key（图层名可能为空或重复，会导致覆盖）。
    返回 (group_dirs, group_members) 供自检输出用。
    """
    group_dirs = {}
    group_members = {}  # {gid: [nm,...]} 供自检

    # 规则1：图层名语义匹配 → 同类整组
    sem_groups = {}  # {sem_type: [layer_indices]}
    remaining = []   # 未匹配语义的图层索引
    for i, l in enumerate(fg_layers):
        sem = _classify_semantic(l.get('nm', ''))
        if sem:
            sem_groups.setdefault(sem, []).append(i)
        else:
            remaining.append(i)

    # 语义组方向：取组内所有元素中心，统一判断
    for sem_type, indices in sem_groups.items():
        gid = f"sem_{sem_type}"
        cx = sum(fg_layers[i]['pos'][0] for i in indices) / len(indices)
        cy = sum(fg_layers[i]['pos'][1] for i in indices) / len(indices)
        d = _get_direction([cx, cy], W, H)
        group_dirs[gid] = d
        group_members[gid] = []
        for i in indices:
            fg_layers[i]['group_id'] = gid
            fg_layers[i]['dir'] = d
            group_members[gid].append(fg_layers[i].get('nm', f'layer_{i}'))

    # 规则2：剩余图层空间聚类
    remaining_layers = [fg_layers[i] for i in remaining]
    clusters = _spatial_cluster(remaining_layers, W)

    for ci, cluster_indices in enumerate(clusters):
        gid = f"cluster_{ci}"
        # cluster_indices 是 remaining_layers 的索引，要映射回 fg_layers
        actual_indices = [remaining[i] for i in cluster_indices]
        # 组方向：取聚类中心
        cx = sum(fg_layers[ai]['pos'][0] for ai in actual_indices) / len(actual_indices)
        cy = sum(fg_layers[ai]['pos'][1] for ai in actual_indices) / len(actual_indices)
        d = _get_direction([cx, cy], W, H)
        group_dirs[gid] = d
        group_members[gid] = []
        for ai in actual_indices:
            fg_layers[ai]['group_id'] = gid
            fg_layers[ai]['dir'] = d
            group_members[gid].append(fg_layers[ai].get('nm', f'layer_{ai}'))

    return group_dirs, group_members

def stage_classify(parse_out, output_dir=None):
    """Stage 1: 静态识别 + 前景分组 + 方向（带 L1 分组 + L2 覆盖）"""
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

    # L1: 纯代码自动分组（直接在图层上设 group_id + dir）
    a_group_dirs, a_group_members = _auto_group(fg_a, W, H)
    b_group_dirs, b_group_members = _auto_group(fg_b, W, H)

    # L2: 人工微调覆盖（如果 group_config.json 存在）
    overrides = _load_group_config(output_dir) if output_dir else None
    has_overrides = False
    if overrides:
        has_overrides = True
        # overrides 按 nm 匹配，覆盖 L1 的分组和方向
        for ov in overrides:
            ov_dir = ov.get('dir')
            ov_nms = set(ov.get('layers', []))
            for l in fg_a + fg_b:
                if l.get('nm', '') in ov_nms:
                    l['dir'] = ov_dir or l['dir']
                    l['group_id'] = 'override'

    # 兜底：确保每个前景元素都有 dir（_auto_group 已设，这里防漏）
    for l in fg_a + fg_b:
        if 'dir' not in l:
            l['dir'] = _get_direction(l['pos'], W, H)
        if 'group_id' not in l:
            l['group_id'] = None

    # 记录分组信息（用于中间产物 + 自检输出）
    return {
        'meta': parse_out['meta'],
        'assets': parse_out['assets'],
        'static_pairs': static_pairs,
        'fg_a': fg_a,
        'fg_b': fg_b,
        'groups_a': a_group_members,
        'groups_b': b_group_members,
        'group_dirs_a': a_group_dirs,
        'group_dirs_b': b_group_dirs,
        'has_overrides': has_overrides,
    }

def stage_classify_check(d):
    """Stage 1 自检"""
    a_inds = [p['a']['ind'] for p in d['static_pairs']]
    b_inds = [p['b']['ind'] for p in d['static_pairs']]
    assert len(a_inds) == len(set(a_inds)), 'static_pairs 中 A 侧有重复 ind'
    assert len(b_inds) == len(set(b_inds)), 'static_pairs 中 B 侧有重复 ind'
    assert d['fg_a'] or d['fg_b'], 'fg_a 和 fg_b 都为空，输入可能有问题'
    # 检查每个前景元素都有 dir
    for l in d['fg_a'] + d['fg_b']:
        assert 'dir' in l, f'图层 {l.get("nm")} 缺少 dir 属性'
    # 输出分组摘要
    all_groups = {}
    for l in d['fg_a'] + d['fg_b']:
        gid = l.get('group_id', 'none')
        all_groups.setdefault(gid, []).append(l['nm'])
    group_summary = ' | '.join(f'{gid}: {",".join(nms[:3])}{"..." if len(nms)>3 else ""}' for gid, nms in all_groups.items())
    override_tag = ' [含人工覆盖]' if d.get('has_overrides') else ''
    _ok(1, f'静态={len(d["static_pairs"])}对 A前景={len(d["fg_a"])} B前景={len(d["fg_b"])} 分组={len(all_groups)}组{override_tag}')
    print(f'    分组详情: {group_summary}')


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 2: Timeline — 时间轴参数 + 每元素时间分配
# ═══════════════════════════════════════════════════════════════════════════════

def _s2f(sec, fps): return round(sec * fps)

def _calc_stagger_ref(i, H, pos_y, fps):
    """参考动效错峰：每元素间隔 0.04-0.10s，高处元素先入场。
    返回错峰帧数（按 fps 换算，不含索引基础值，由调用方叠加）"""
    bonus = max(0, int((H - pos_y) / 180))
    return _s2f(i * 0.06, fps) + bonus  # i*0.06s 约 1-2f 间隔（30fps）

# ── 参考时长（秒），按 30fps 视觉标定，自动适配任意 fps ──────────────────────
# 核心原则：所有运动时长以"秒"定义，再按 fps 换算成帧。
# 这样 30fps / 60fps / 100fps 下的视觉节奏完全一致。
T_IN_DUR    = (0.22, 0.35)   # 入场 0.22-0.35s（30fps 下约 7-10f）
T_OUT_DUR   = (0.22, 0.45)   # 退场 0.22-0.45s（30fps 下约 7-13f）
T_STAGGER   = (0.04, 0.10)   # 错峰 0.04-0.10s（30fps 下约 1-3f）
T_CROSS_WIN = 0.20           # 交叉窗口 0.20s（30fps 下约 6f）
T_BOUNCE    = 0.15           # bounce 回弹 0.15s（偏柔，避免太硬）
T_WINDUP    = 0.08           # 蓄力 0.08s（确保任何 fps 下都可见）
T_WINDUP_MIN = 0.03          # 蓄力最小时长 0.03s（退场总时长极短时的兜底下限，按 fps 换算）
T_MIN_HOLD  = 0.60           # 最小展示时长 0.6s（入场后至少停留这么久才退场）
T_FADE_DECO = 0.07           # 装饰类淡入淡出 0.07s
T_FADE_STD  = 0.10           # 普通/商品类淡入淡出 0.10s

def stage_timeline(classify_out, params=None):
    """Stage 2: 参考动效风格时间轴
    结构：两段式交叉切换 + 首尾空帧循环
    - A组: t=0 从屏幕外入场 → 展示 → 退场到屏幕外
    - 交叉窗口: A退场 + B入场 同步
    - B组: t≈T/2 从屏幕外入场 → 展示 → 退场到屏幕外
    - 首尾空帧: t=0 和 t=F_TOTAL 元素都在屏幕外
    所有时长以秒定义，按 fps 换算成帧，保证不同 fps 下节奏一致。
    """
    import random
    meta = classify_out['meta']
    fps = meta['fr']
    H = meta['h']

    # 默认时间轴参数（秒）
    p = {
        'T_TOTAL': 5.0,        # 总时长 5s
        'T_A_IN_START': 0.0,   # A组入场开始（首帧空帧，从屏幕外开始飞入）
        'T_CROSS': 2.4,        # 交叉切换中心点（A退场+B入场 同窗口）
        'T_CROSS_WIN': T_CROSS_WIN,  # 交叉窗口时长（秒）
    }
    if params:
        p.update(params)

    F_TOTAL = _s2f(p['T_TOTAL'], fps)
    F_A_IN_START = _s2f(p['T_A_IN_START'], fps)
    F_CROSS = _s2f(p['T_CROSS'], fps)
    CROSS_WIN = _s2f(p['T_CROSS_WIN'], fps)

    # 交叉窗口：A退场开始 = B入场开始 = F_CROSS - CROSS_WIN/2
    half_cross = CROSS_WIN // 2
    F_A_EXIT_S = F_CROSS - half_cross   # A开始退场（B同时开始入场）
    F_A_EXIT_E = F_CROSS + half_cross   # A退场结束
    # B入场窗口 = A退场窗口
    F_B_IN_S = F_A_EXIT_S
    F_B_IN_E = F_A_EXIT_E
    # B退场窗口在末尾，紧贴 F_TOTAL（确保有足够展示时间）
    F_B_EXIT_S = F_TOTAL - half_cross - _s2f(T_OUT_DUR[0], fps)  # 留退场窗口（与 A 对称）
    F_B_EXIT_E = F_TOTAL

    static_pairs = classify_out['static_pairs']
    static_sorted = sorted([pp['a'] for pp in static_pairs], key=lambda l: l['ind'])
    static_top = [l for l in static_sorted if l['ind'] < 5]
    static_bot = [l for l in static_sorted if l['ind'] >= 5]

    # 秒 → 帧换算（用于每元素时间分配）
    in_dur_range_f  = (_s2f(T_IN_DUR[0], fps), _s2f(T_IN_DUR[1], fps))
    out_dur_range_f = (_s2f(T_OUT_DUR[0], fps), _s2f(T_OUT_DUR[1], fps))
    stagger_range_f = (_s2f(T_STAGGER[0], fps), _s2f(T_STAGGER[1], fps))
    # 展示最小时长（秒→帧），确保入场后有足够停留
    min_hold_f = _s2f(T_MIN_HOLD, fps)

    def assign_timeline(base_start, exit_s, exit_e, layers, tag):
        """为每个元素分配独立时间轴，确保 in_end <= hold_end"""
        timeline = []
        cursor = base_start
        for i, l in enumerate(sorted(layers, key=lambda l: l['ind'])):
            # 入场时长：不能超过 exit_s - cursor - min_hold（至少留 min_hold 展示）
            max_in = max(in_dur_range_f[0], exit_s - cursor - min_hold_f)
            in_dur = min(random.randint(*in_dur_range_f), max_in)
            in_start = cursor
            in_end = in_start + in_dur
            hold_end = exit_s
            # 退场时长：不能超过 F_TOTAL - hold_end
            max_out = max(out_dur_range_f[0], F_TOTAL - hold_end)
            out_dur = min(random.randint(*out_dur_range_f), max_out)
            out_start = hold_end
            out_end = min(out_start + out_dur, F_TOTAL)

            timeline.append({
                'layer': l,
                'in_start': in_start,
                'in_end': in_end,
                'hold_end': hold_end,
                'out_start': out_start,
                'out_end': out_end,
                'initially_visible': (tag == 'a'),  # A组首帧位置在屏幕外但opacity=100
                'stagger': 0,  # 错峰已通过 cursor 递增实现
            })
            # 下一个元素错峰
            cursor = in_start + random.randint(*stagger_range_f)
        return timeline

    a_timeline = assign_timeline(F_A_IN_START, F_A_EXIT_S, F_A_EXIT_E, classify_out['fg_a'], 'a')
    b_timeline = assign_timeline(F_B_IN_S, F_B_EXIT_S, F_B_EXIT_E, classify_out['fg_b'], 'b')

    return {
        'meta': meta,
        'assets': classify_out['assets'],
        'timeline_params': {
            'F_TOTAL': F_TOTAL, 'F_A_EXIT_S': F_A_EXIT_S, 'F_A_EXIT_E': F_A_EXIT_E,
            'F_B_IN_S': F_B_IN_S, 'F_B_IN_E': F_B_IN_E,
            'F_B_EXIT_S': F_B_EXIT_S, 'F_B_EXIT_E': F_B_EXIT_E,
            'CROSS_WIN': CROSS_WIN,
        },
        'static_top': static_top,
        'static_bot': static_bot,
        'b_timeline': b_timeline,
        'a_timeline': a_timeline,
    }

def stage_timeline_check(d):
    """Stage 2 自检（参考动效风格时间轴）"""
    tp = d['timeline_params']
    F = tp['F_TOTAL']
    # 交叉窗口必须合理
    assert tp['F_A_EXIT_S'] < tp['F_A_EXIT_E'], 'A退场窗口起点必须 < 终点'
    assert tp['F_B_EXIT_S'] < tp['F_B_EXIT_E'], 'B退场窗口起点必须 < 终点'
    assert tp['F_A_EXIT_E'] <= tp['F_B_EXIT_S'], 'A退场结束必须 <= B退场开始（两段不重叠）'
    assert tp['F_B_EXIT_E'] <= F, f'B退场结束({tp["F_B_EXIT_E"]}) 必须 <= F_TOTAL({F})'
    # 每个元素时间轴合法
    for label, tl in [('A', d['a_timeline']), ('B', d['b_timeline'])]:
        for item in tl:
            assert item['in_start'] < item['in_end'], f'{label} 入场窗口起点必须 < 终点'
            assert item['in_end'] <= item['hold_end'], f'{label} 入场结束必须 <= 展示结束'
            assert item['out_start'] <= item['out_end'], f'{label} 退场窗口起点必须 < 终点'
            assert item['out_end'] <= F, f'{label} 退场结束({item["out_end"]}) > F_TOTAL({F})'
    _ok(2, f'时间轴 F={F} 交叉={tp["F_A_EXIT_S"]}→{tp["F_A_EXIT_E"]} B退={tp["F_B_EXIT_S"]}→{tp["F_B_EXIT_E"]}')


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 3: Keyframes — 参考动效风格（位移飞入飞出 + 旋转 + 错峰）
# 缓动：ease-in-out (0.33,0,0.67,1) 标准 + (0.167,0.1,0.667,1) 装饰弹性
# 空帧策略：A组 opacity=100 恒定（位置控制可见性），B组 opacity 动画
# ═══════════════════════════════════════════════════════════════════════════════

# 参考动效缓动曲线
EASE_STD_O    = {"x": [0.33], "y": [0]}       # 标准 ease-in-out 出
EASE_STD_I    = {"x": [0.67], "y": [1]}       # 标准 ease-in-out 入
EASE_BOUNCE_O = {"x": [0.167], "y": [0.1]}    # 装饰弹性出
EASE_BOUNCE_I = {"x": [0.667], "y": [1]}      # 装饰弹性入

def _is_decoration(nm):
    """判断是否装饰元素（气球/星星等）"""
    keywords = ['气球', '星星', '五角星', 'joy', '星']
    return any(k in nm.lower() for k in keywords)

def _kf(t, s, ei=None, eo=None):
    if ei is None: ei = EASE_STD_I
    if eo is None: eo = EASE_STD_O
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
    if direction == "left":
        dx_in = (-margin) - vr
        return (dx_in, 0)
    elif direction == "right":
        dx_in = (W + margin) - vl
        return (dx_in, 0)
    elif direction == "bottom":
        dy_in = (H + margin) - vt
        return (0, dy_in)
    elif direction == "top":
        dy_in = (-margin) - vb
        return (0, dy_in)
    else:
        # center: 从下方飞入
        return (0, (H + margin) - vt)

def _build_pos_kfs_style(l, in_start, in_end, out_start, out_end, W, H, fps):
    """参考动效风格 position 关键帧
    入场: 蓄力(屏幕外) → 飞入+overshoot → bounce回弹
    退场: 蓄力(画面内,往反方向退) → 飞出
    所有时长以秒定义再按 fps 换算，保证不同 fps 下节奏一致。
    """
    x, y, z = l['pos']
    ax, ay = l['anc'][0], l['anc'][1]
    sx, sy = l['scl'][0] / 100.0, l['scl'][1] / 100.0
    aw, ah = l.get('aw', 100), l.get('ah', 100)
    dx, dy = _get_flight_distance(x, y, l['dir'], aw, ah, ax, ay, sx, sy, W, H)
    entry_x, entry_y = x + dx, y + dy  # 屏幕外位置

    is_deco = _is_decoration(l.get('nm', ''))
    ei = EASE_BOUNCE_I if is_deco else EASE_STD_I
    eo = EASE_BOUNCE_O if is_deco else EASE_STD_O

    # ── 入场参数 ──
    os_ratio = 0.12 if is_deco else 0.08       # overshoot 幅度
    windup_ratio = 0.15                          # 蓄力幅度
    # 入场蓄力：往屏幕外方向多移
    in_windup_x = x + dx * (1 + windup_ratio)
    in_windup_y = y + dy * (1 + windup_ratio)
    # 入场 overshoot：飞入超过目标（朝画面内方向多走）
    os_x = x - dx * os_ratio
    os_y = y - dy * os_ratio

    # bounce / windup 时长（秒→帧）
    bounce_dur_f = _s2f(T_BOUNCE, fps)
    t_bounce = min(in_end + bounce_dur_f, out_start - 1)

    windup_dur_f = _s2f(T_WINDUP, fps)

    # ── 退场参数 ──
    # 退场蓄力：往画面内方向偏移（飞出方向的反方向），让观众看到"准备弹射"
    out_windup_x = x - dx * windup_ratio   # dx 是飞出方向，-dx 是画面内方向
    out_windup_y = y - dy * windup_ratio
    # 退场蓄力时长（秒→帧），下限用 T_WINDUP_MIN 按 fps 换算，确保不为 0
    windup_min_f = _s2f(T_WINDUP_MIN, fps)
    out_windup_dur_f = min(windup_dur_f, max(windup_min_f, (out_end - out_start) // 3))
    out_windup_t = out_start + out_windup_dur_f

    def p3(px, py): return [px, py, z]
    kfs = []

    # === 入场段 ===
    if in_start > 0:
        kfs.append(_kf(0, p3(entry_x, entry_y), ei, eo))  # 首帧空帧

    if in_start >= windup_dur_f:
        # 有完整蓄力：先静止在屏幕外，再往更远处退（蓄力），然后飞入
        kfs.append(_kf(in_start - windup_dur_f, p3(entry_x, entry_y),    ei, eo))  # 蓄力起点（静止在屏幕外）
        kfs.append(_kf(in_start,              p3(in_windup_x, in_windup_y), ei, eo))  # 蓄力完成（往后退）
    elif in_start > 0:
        # in_start 太小，蓄力从 t=0 开始（空帧已在上面添加）
        kfs.append(_kf(in_start, p3(in_windup_x, in_windup_y), ei, eo))  # 蓄力完成
    else:
        # A组首元素 in_start=0：从 t=0 在屏幕外，蓄力到 windup_dur_f，再飞入
        kfs.append(_kf(0,             p3(entry_x, entry_y),    ei, eo))  # 蓄力起点
        kfs.append(_kf(windup_dur_f,  p3(in_windup_x, in_windup_y), ei, eo))  # 蓄力完成

    kfs.append(_kf(in_end,   p3(os_x, os_y), ei, eo))    # 飞入 overshoot（冲过头）
    kfs.append(_kf(t_bounce, p3(x, y),      ei, eo))     # bounce 回弹到位

    # === 展示段 ===
    kfs.append(_kf(out_start, p3(x, y), ei, eo))          # 展示结束

    # === 退场段（带蓄力） ===
    kfs.append(_kf(out_windup_t, p3(out_windup_x, out_windup_y), ei, eo))  # 退场蓄力（往画面内退）
    kfs.append(_kf(out_end,      p3(entry_x, entry_y),         ei, eo))    # 飞出到屏幕外

    return sorted(kfs, key=lambda k: k['t'])

def _build_rot_kfs_style(l, in_start, in_end, out_start, out_end, base_rot, fps):
    """参考动效风格 rotation 关键帧
    入场: 蓄力反转 → 飞入overshoot → bounce回正
    退场: 蓄力反转 → 飞出
    所有时长以秒定义再按 fps 换算。
    """
    is_deco = _is_decoration(l.get('nm', ''))
    rot_offset = 15 if is_deco else 8
    ei = EASE_BOUNCE_I if is_deco else EASE_STD_I
    eo = EASE_BOUNCE_O if is_deco else EASE_STD_O

    # 入场蓄力：往入场反方向多转
    in_windup_rot = base_rot + rot_offset * 1.3
    # 入场 overshoot
    os_rot = base_rot - rot_offset * 0.3

    # bounce / windup 时长（秒→帧）
    bounce_dur_f = _s2f(T_BOUNCE, fps)
    t_bounce = min(in_end + bounce_dur_f, out_start - 1)

    windup_dur_f = _s2f(T_WINDUP, fps)
    windup_min_f = _s2f(T_WINDUP_MIN, fps)
    out_windup_dur_f = min(windup_dur_f, max(windup_min_f, (out_end - out_start) // 3))
    out_windup_t = out_start + out_windup_dur_f

    # 退场蓄力：往退场反方向多转（即往入场方向转一点，像拧弹簧）
    out_windup_rot = base_rot + rot_offset * 0.4

    kfs = []
    if in_start > 0:
        kfs.append(_kf(0, [base_rot + rot_offset], ei, eo))  # 首帧

    # === 入场段 ===
    if in_start >= windup_dur_f:
        kfs.append(_kf(in_start - windup_dur_f, [base_rot + rot_offset], ei, eo))  # 蓄力起点
        kfs.append(_kf(in_start,              [in_windup_rot], ei, eo))             # 蓄力完成
    elif in_start > 0:
        kfs.append(_kf(in_start, [in_windup_rot], ei, eo))
    else:
        kfs.append(_kf(0,            [base_rot + rot_offset], ei, eo))  # 蓄力起点
        kfs.append(_kf(windup_dur_f, [in_windup_rot], ei, eo))           # 蓄力完成

    kfs.append(_kf(in_end,   [os_rot],     ei, eo))    # overshoot
    kfs.append(_kf(t_bounce, [base_rot],   ei, eo))    # bounce 回正

    # === 展示段 ===
    kfs.append(_kf(out_start, [base_rot], ei, eo))     # 展示结束

    # === 退场段（带蓄力） ===
    kfs.append(_kf(out_windup_t, [out_windup_rot],            ei, eo))  # 退场蓄力（反向转）
    kfs.append(_kf(out_end,      [base_rot - rot_offset],     ei, eo))  # 飞出

    return sorted(kfs, key=lambda k: k['t'])

def _build_opa_kfs_style(l, in_start, in_end, out_start, out_end, is_a_group, fps):
    """参考动效风格 opacity 关键帧
    A组: opacity 恒定 100（位置控制空帧，避免渲染器兼容性问题）
    B组: opacity 动画（快速淡入淡出，装饰类更快）
    淡入淡出时长以秒定义再按 fps 换算。
    """
    if is_a_group:
        # A组：恒定 100，纯靠位置控制可见性
        return None  # 返回 None 表示用静态 opacity

    # B组：快速淡入 → 展示 → 快速淡出
    is_deco = _is_decoration(l.get('nm', ''))
    # 淡入时长（秒→帧）：装饰类 T_FADE_DECO，普通类 T_FADE_STD
    fade_f = _s2f(T_FADE_DECO if is_deco else T_FADE_STD, fps)
    ei = EASE_BOUNCE_I if is_deco else EASE_STD_I
    eo = EASE_BOUNCE_O if is_deco else EASE_STD_O

    kfs = []
    if in_start > 0:
        kfs.append(_kf(0, [0], ei, eo))  # 首帧空帧
    kfs.append(_kf(in_start,  [0],    ei, eo))  # 入场起点
    kfs.append(_kf(in_start + fade_f, [100], ei, eo))  # 快速淡入
    kfs.append(_kf(out_end - fade_f,  [100], ei, eo))  # 展示
    kfs.append(_kf(out_end,  [0],    ei, eo))  # 退场淡出
    return sorted(kfs, key=lambda k: k['t'])

def _make_anim_layer_style(l, tag, in_start, in_end, out_start, out_end, W, H, F_TOTAL, fps):
    """参考动效风格动画层：位移 + 旋转 + opacity策略"""
    pos_kfs = _build_pos_kfs_style(l, in_start, in_end, out_start, out_end, W, H, fps)
    rot_kfs = _build_rot_kfs_style(l, in_start, in_end, out_start, out_end, l['rot'], fps)
    is_a = (tag == 'a')
    opa_kfs = _build_opa_kfs_style(l, in_start, in_end, out_start, out_end, is_a, fps)

    layer = _layer_base(l, tag, F_TOTAL)
    if opa_kfs is None:
        # A组：opacity 静态 100
        layer["ks"] = {
            "o": {"a": 0, "k": 100},
            "r": {"a": 1, "k": rot_kfs},
            "p": {"a": 1, "k": pos_kfs},
            "a": {"a": 0, "k": l['anc']},
            "s": {"a": 0, "k": l['scl']},
        }
    else:
        # B组：opacity 动画
        layer["ks"] = {
            "o": {"a": 1, "k": opa_kfs},
            "r": {"a": 1, "k": rot_kfs},
            "p": {"a": 1, "k": pos_kfs},
            "a": {"a": 0, "k": l['anc']},
            "s": {"a": 0, "k": l['scl']},
        }
    return layer

def stage_keyframes(timeline_out):
    """Stage 3: 参考动效风格关键帧生成"""
    meta = timeline_out['meta']
    W, H = meta['w'], meta['h']
    fps = meta['fr']
    F_TOTAL = timeline_out['timeline_params']['F_TOTAL']
    out_layers = []

    # 静态顶层
    for l in timeline_out['static_top']:
        out_layers.append(_make_static_layer(l, 'a', F_TOTAL))
    # B 前景
    for item in timeline_out['b_timeline']:
        l = item['layer']
        out_layers.append(_make_anim_layer_style(l, 'b',
            item['in_start'], item['in_end'], item['out_start'], item['out_end'], W, H, F_TOTAL, fps))
    # A 前景
    for item in timeline_out['a_timeline']:
        l = item['layer']
        out_layers.append(_make_anim_layer_style(l, 'a',
            item['in_start'], item['in_end'], item['out_start'], item['out_end'], W, H, F_TOTAL, fps))
    # 静态底层
    for l in timeline_out['static_bot']:
        out_layers.append(_make_static_layer(l, 'a', F_TOTAL))

    return {
        'meta': meta,
        'assets': timeline_out['assets'],
        'timeline_params': timeline_out['timeline_params'],
        'layers': out_layers,
    }

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

# 旧的 _make_anim_layer 和 stage_keyframes 已被 _make_anim_layer_style 和新版 stage_keyframes 替代
# （新版在下方，使用参考动效风格的时间轴字段 in_start/in_end/out_start/out_end）

def stage_keyframes_check(d):
    """Stage 3 自检：关键帧时间戳递增 + 维度正确 + opacity 策略 + 空帧验证"""
    issues = []
    for i, l in enumerate(d['layers']):
        nm = l.get('nm', f'layer_{i}')
        ks = l.get('ks', {})
        for prop in ['o', 'r', 'p']:
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
    # opacity 策略验证：A组 opacity 必须静态(a=0,k=100)，B组 opacity 必须动画(a=1)
    for i, l in enumerate(d['layers']):
        nm = l.get('nm', f'layer_{i}')
        ks = l.get('ks', {})
        o_pk = ks.get('o', {})
        p_pk = ks.get('p', {})
        # 只检查动画层（p 有动画的）
        if p_pk.get('a') == 1:
            tag = l.get('_tag', '')
            if tag == 'a':
                # A组：opacity 应该静态 100
                if o_pk.get('a') != 0 or o_pk.get('k') != 100:
                    issues.append(f'{nm} A组 opacity 应为静态100，实际 a={o_pk.get("a")} k={o_pk.get("k")}')
            elif tag == 'b':
                # B组：opacity 应该有动画
                if o_pk.get('a') != 1:
                    issues.append(f'{nm} B组 opacity 应为动画，实际 a={o_pk.get("a")}')
    if issues:
        _fail(3, f'{len(issues)} 个关键帧问题: {issues[:5]}...')
    _ok(3, f'关键帧生成 {len(d["layers"])} 层，时间戳+维度+opacity策略全部合法')


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
        # 计算 JSON 文件大小（bytes → KB/MB）
        size_bytes = len(json_str.encode('utf-8'))
        if size_bytes >= 1024 * 1024:
            size_str = f'{size_bytes / 1024 / 1024:.1f} MB'
        else:
            size_str = f'{size_bytes / 1024:.0f} KB'
        json_line = f'var jsonData = {json_str};\nwindow.__JSON_SIZE__ = "{size_str}";'
        bootstrap = """st.textContent = 'Lottie库加载中...';
loadCdn(function(err) {
  if (err) { st.style.color = '#f88'; st.textContent = '❌ Lottie库加载失败: ' + err.message; return; }
  if (typeof lottie === 'undefined') { st.style.color = '#f88'; st.textContent = '❌ Lottie对象未定义'; return; }
  initAnimation();
  loadFileSaver(function() {});
});"""
    else:
        title = 'Lottie 切换动效预览'
        json_line = 'var jsonData = null;\nwindow.__JSON_SIZE__ = "";'
        bootstrap = """var ts = new Date().getTime();
st.textContent = '正在加载动画数据...';
fetch('merged_output.json?t=' + ts)
  .then(function(r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  })
  .then(function(d) {
    jsonData = d;
    // 计算 fetch 到的 JSON 大小
    var sizeBytes = JSON.stringify(d).length;
    if (sizeBytes >= 1024 * 1024) { window.__JSON_SIZE__ = (sizeBytes / 1024 / 1024).toFixed(1) + ' MB'; }
    else { window.__JSON_SIZE__ = Math.round(sizeBytes / 1024) + ' KB'; }
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
    anim.addEventListener('enterFrame', function() { fi.textContent = '帧: ' + Math.round(anim.currentFrame) + ' / ' + anim.totalFrames + '  |  JSON: ' + window.__JSON_SIZE__; });
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
    """Stage 5 自检：FileSaver 逻辑齐全 + JSON 大小显示"""
    for label, path in [('fetch', preview_fetch), ('embedded', preview_embedded)]:
        with open(path, encoding='utf-8') as f:
            content = f.read()
        checks = {
            'saveAs调用': 'saveAs(blob' in content,
            'FileSaver CDN': 'file-saver' in content,
            'loadFileSaver函数': 'function loadFileSaver' in content,
            '降级方案': 'URL.createObjectURL' in content,
            'JSON大小显示': '__JSON_SIZE__' in content,
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
        classify_out = stage_classify(parse_out, output_dir)
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
