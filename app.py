"""纸张对折打孔展开预览 —— Flask 后端。

几何模型
========
- 原纸坐标系：原点 (0,0) 在左下角，x 向右、y 向上，单位 mm。
- 每次对折沿当前折叠面的水平或垂直中线，当前面对应尺寸减半。
- 折叠后逐条跟踪当前面四条边的身份：
  * "cut"  —— 开口边（对应原纸边界）
  * "fold" —— 折痕边
  重要不变量：历史上所有折痕最终都落在当前折叠面的边界上。因此打孔点只要
  与当前面四条边界的距离都大于打孔半径（带容差），展开后所有孔就不会压在
  原纸边界或任何折痕上，结果唯一、不含糊。
- 展开：按折叠顺序逆推（物理 mm 坐标）。每展开一层，静止层恒等映射，
  翻折层关于该层中线镜像；最终得到原纸上的全部孔位并按坐标去重。
- 反查打孔：把原纸上的目标孔心按折叠顺序正推（静止半恒等、翻折半关于
  中线镜像），逐层折回最终折叠面，得到归一化孔心；再用同一展开算法列出
  这一次打孔必然生成的全部孔位。目标孔心距纸张边界/折痕不大于打孔半径时
  拒绝并说明具体原因。
- 目标孔位排版：把原纸上的一批目标孔心分别折回最终折叠面（多目标可折到
  同一打孔点），再展开为完整连带孔组并按坐标容差去重。只有连带孔集合与
  目标集合完全一致、方案打孔点数不超过 MAX_HOLES 且通过边界/折痕/保护区
  校验时才返回可用方案；不可实现时指出缺少的连带孔或违规目标及原因。
- 全部折痕展开后构成 (2^nv - 1) x (2^nh - 1) 的等分网格。
- 原纸保护区：原纸上至多 MAX_ZONES 个轴对齐矩形（mm 坐标），绘入展开图。
  任一展开孔圆与任一保护区相交或相切即违规：指出原打孔点、触碰的展开孔
  及保护区，不生成展开 SVG、禁止下载；反查打孔的待确认结果同样判定。
  保护区必须完整位于原纸内，纸张尺寸改动导致越界时提示修正。
- 最小纸桥宽度：可选参数 minBridge（mm，0 表示关闭）。启用后服务端先按
  现有坐标容差合并展开孔位，再用空间网格检查任意两个不同孔的圆边净距
  （圆心距 - 2r）：净距小于设定宽度即冲突，相等可通过；返回首个冲突对的
  两孔坐标与实际净距。冲突时不生成展开 SVG、禁止下载，反查待确认结果
  无法确认、排版方案不可用。反查按「加入现有孔位后的全集」判定，
  排版按「替换后的方案」判定。
- A4 分页定位模板：合法展开结果可另行打包为纵向 A4（210×297mm）分页
  SVG 的 ZIP。按实际毫米 1:1 排位（不缩放），四边留白 10mm、相邻页
  物理重叠 15mm；按原纸尺寸计算覆盖全纸的分页窗口，每页用裁剪路径裁切
  纸边、折痕、保护区与孔圆，跨页图形在相邻页各自保留窗口内片段；每页
  标注页号/行列号（行号自下而上，与原纸左下角原点方向一致）、原纸窗口
  mm 坐标与重叠区对位十字（相邻页同坐标各画一份，打印后重合对齐）。
  分页入口与展开图下载共用全部孔位/保护区/纸桥校验，任一违规即拒绝。
"""

from __future__ import annotations

import io
import math
import os
import zipfile

from flask import Flask, jsonify, render_template, request, Response

app = Flask(__name__)

# ---------------------------------------------------------------------------
# 配置（可用环境变量覆盖）
# ---------------------------------------------------------------------------
MAX_FOLDS = int(os.environ.get("MAX_FOLDS", "8"))
MAX_HOLES = int(os.environ.get("MAX_HOLES", "24"))
MAX_ZONES = int(os.environ.get("MAX_ZONES", "8"))
# 单次排版的目标孔心上限，默认与「打孔点上限 × 单点最多展开孔数」一致
MAX_TARGETS = int(os.environ.get(
    "MAX_TARGETS", str(MAX_HOLES * 2 ** MAX_FOLDS)))
MIN_DIM = 20.0          # mm
MAX_DIM = 2000.0        # mm
MIN_RADIUS = 0.5        # mm
MAX_RADIUS = 50.0       # mm
MIN_BRIDGE = 0.0        # mm，最小纸桥宽度下限（0 表示关闭该检查）
MAX_BRIDGE = MAX_DIM    # mm，最小纸桥宽度上限
EDGE_TOL = 0.05         # mm，边界判定容差（浮点误差 + 必须严格在内侧）
ROUND_TRIP_TOL = 1e-6   # mm，归一化坐标往返（mm -> 归一化 -> mm）的浮点误差宽限：
                        # 反查/排版给出的孔心确认后重算时，不被同一校验误拒

# A4 纵向分页定位模板（单位 mm，实际毫米 1:1，绝不缩放）
A4_W = 210.0
A4_H = 297.0
A4_MARGIN = 10.0        # 页面四边留白
A4_OVERLAP = 15.0       # 相邻页物理重叠宽度
A4_STEP_X = A4_W - 2 * A4_MARGIN - A4_OVERLAP   # x 向相邻窗口步进 175mm
A4_STEP_Y = A4_H - 2 * A4_MARGIN - A4_OVERLAP   # y 向相邻窗口步进 262mm
A4_MAX_PAGES = 128      # 分页上限（2000×2000mm 原纸最多 12×8=96 页）

ALLOWED_FOLDS = ("vL", "vR", "hT", "hB")
EDGE_NAMES = {
    "fold": {"x0": "左侧折痕边", "x1": "右侧折痕边",
             "y0": "下侧折痕边", "y1": "上侧折痕边"},
    "cut": {"x0": "纸张左边界", "x1": "纸张右边界",
            "y0": "纸张下边界", "y1": "纸张上边界"},
}


# ---------------------------------------------------------------------------
# 折叠模型
# ---------------------------------------------------------------------------
def folded_state(width, height, folds):
    """按折叠顺序推进，返回 (当前面宽, 当前面高, 四条边身份, 垂直/水平次数)。"""
    w, h = float(width), float(height)
    # 初始四条边都是原纸开口边界
    edges = {"x0": "cut", "x1": "cut", "y0": "cut", "y1": "cut"}
    nv = nh = 0
    for f in folds:
        if f in ("vL", "vR"):
            w /= 2.0
            nv += 1
            # 垂直对折：折痕落在当前面中线上。vR 右半折向左，折痕边是当前面
            # 右边（x1）；vL 左半折向右，折痕边是当前面左边（x0）。
            edges["x1" if f == "vR" else "x0"] = "fold"
        else:
            h /= 2.0
            nh += 1
            # 水平对折：hT 上半折向下，折痕边是当前面上边（y1）；
            # hB 下半折向上，折痕边是当前面下边（y0）。
            edges["y0" if f == "hB" else "y1"] = "fold"
    return w, h, edges, nv, nh


def unfold_points(points_mm, folds, width, height):
    """逆推孔位。输入最终折叠面局部 mm 坐标（原点为该面左下角），
    返回原纸 mm 坐标列表。

    逆推必须使用物理坐标：每展开一层，面宽/面高翻倍，中线位置随之变化，
    归一化坐标无法表达这一点。
    """
    cur = [(float(p["x"]), float(p["y"])) for p in points_mm]
    cw = width / (2.0 ** sum(1 for f in folds if f in ("vL", "vR")))
    ch = height / (2.0 ** sum(1 for f in folds if f in ("hT", "hB")))
    for f in reversed(folds):
        nxt = []
        if f in ("vL", "vR"):
            pw = 2.0 * cw          # 展开前一面的宽度
            line = pw / 2.0
            if f == "vR":          # 右侧折向左：静止左半恒等，右半镜像
                for x, y in cur:
                    nxt.append((x, y))
                    nxt.append((2.0 * line - x, y))
            else:                  # vL 左侧折向右：静止右半，左半镜像
                for x, y in cur:
                    nxt.append((line + x, y))
                    nxt.append((line - x, y))
            cw = pw
        else:
            ph = 2.0 * ch
            line = ph / 2.0
            if f == "hT":          # 上侧折向下：静止下半恒等，上半镜像
                for x, y in cur:
                    nxt.append((x, y))
                    nxt.append((x, 2.0 * line - y))
            else:                  # hB 下侧折向上：静止上半，下半镜像
                for x, y in cur:
                    nxt.append((x, line + y))
                    nxt.append((x, line - y))
            ch = ph
        cur = nxt
    return cur


def fold_point_to_face(tx, ty, folds, width, height):
    """正推：把原纸 mm 坐标的目标点按折叠顺序逐层折回最终折叠面。

    与 unfold_points 互为逆映射：每折一次，静止半不变，翻折半关于该层
    中线镜像，当前面对应尺寸减半。返回折叠面局部 mm 坐标。
    """
    x, y = float(tx), float(ty)
    cw, ch = float(width), float(height)
    for f in folds:
        if f in ("vL", "vR"):
            line = cw / 2.0
            # vR 右半折向左：x -> line - |x - line|；vL 左半折向右：x -> |x - line|
            x = line - abs(x - line) if f == "vR" else abs(x - line)
            cw = line
        else:
            line = ch / 2.0
            # hT 上半折向下：y -> line - |y - line|；hB 下半折向上：y -> |y - line|
            y = line - abs(y - line) if f == "hT" else abs(y - line)
            ch = line
    return x, y


def dedupe_points(points, tol=1e-5):
    """按物理 mm 坐标去重（重合位置只保留一个）。

    用 tol 网格索引加速：距离不超过 tol 的两点必落在相邻九宫格内，
    判定结果与逐对比较一致，但排版场景（数千个孔）不至于平方级耗时。
    """
    cells = {}
    out = []
    for x, y in points:
        cx, cy = math.floor(x / tol), math.floor(y / tol)
        dup = False
        for ix in (cx - 1, cx, cx + 1):
            for iy in (cy - 1, cy, cy + 1):
                for qx, qy in cells.get((ix, iy), ()):
                    if abs(x - qx) <= tol and abs(y - qy) <= tol:
                        dup = True
                        break
                if dup:
                    break
            if dup:
                break
        if dup:
            continue
        cells.setdefault((cx, cy), []).append((x, y))
        out.append((x, y))
    return [{"x": round(x, 6), "y": round(y, 6)} for x, y in out]


def point_set_membership(points, tol=1e-5):
    """把点集按 tol 网格索引，返回成员判定函数（|dx|、|dy| 均不超过 tol）。"""
    cells = {}
    for x, y in points:
        key = (math.floor(x / tol), math.floor(y / tol))
        cells.setdefault(key, []).append((x, y))

    def contains(px, py):
        cx, cy = math.floor(px / tol), math.floor(py / tol)
        for ix in (cx - 1, cx, cx + 1):
            for iy in (cy - 1, cy, cy + 1):
                for qx, qy in cells.get((ix, iy), ()):
                    if abs(px - qx) <= tol and abs(py - qy) <= tol:
                        return True
        return False

    return contains


def dedupe_with_index(points, tol=1e-5):
    """按物理 mm 坐标去重，返回 (唯一点列表, 原点下标 -> 唯一点下标)。"""
    cells = {}
    uniq = []
    index = []
    for x, y in points:
        cx, cy = math.floor(x / tol), math.floor(y / tol)
        found = -1
        for ix in (cx - 1, cx, cx + 1):
            for iy in (cy - 1, cy, cy + 1):
                for gi, (qx, qy) in cells.get((ix, iy), ()):
                    if abs(x - qx) <= tol and abs(y - qy) <= tol:
                        found = gi
                        break
                if found >= 0:
                    break
            if found >= 0:
                break
        if found < 0:
            found = len(uniq)
            uniq.append((x, y))
            cells.setdefault((cx, cy), []).append((found, (x, y)))
        index.append(found)
    return uniq, index


def check_point(px, py, w, h, radius, edges):
    """校验单个打孔点，返回中文错误信息；合法返回 None。"""
    r = radius + EDGE_TOL - ROUND_TRIP_TOL
    checks = (("x0", px), ("x1", w - px), ("y0", py), ("y1", h - py))
    for key, dist in checks:
        if dist < r:
            kind = edges[key]
            return (f"孔位 ({px:.2f}, {py:.2f}) 距{EDGE_NAMES[kind][key]}仅 "
                    f"{max(dist, 0.0):.2f}mm，小于打孔半径 {radius:g}mm"
                    f"（{'折痕' if kind == 'fold' else '纸张边界'}，"
                    f"孔必须完整落在纸面内侧），请调整孔位。")
    return None


def check_target_on_paper(tx, ty, width, height, radius, nv, nh):
    """校验原纸上的目标孔心：打孔半径不能触及纸张边界或任何折痕。

    与折叠面上的 check_point 几何上等价（折叠就是把边界/折痕网格
    折到当前面四边），这里从原纸视角给出更直观的中文原因。
    """
    r = radius + EDGE_TOL
    bounds = (("纸张左边界", tx), ("纸张右边界", width - tx),
              ("纸张下边界", ty), ("纸张上边界", height - ty))
    for name, dist in bounds:
        if dist < r:
            return (f"目标孔心 ({tx:.2f}, {ty:.2f}) 距{name}仅 "
                    f"{max(dist, 0.0):.2f}mm，小于打孔半径 {radius:g}mm"
                    "（孔会超出纸张边界），请把目标点移离边界。")
    for k in range(1, 2 ** nv):
        pos = width * k / 2 ** nv
        dist = abs(tx - pos)
        if dist < r:
            return (f"目标孔心 ({tx:.2f}, {ty:.2f}) 距垂直折痕 x={pos:.2f}mm 仅 "
                    f"{dist:.2f}mm，小于打孔半径 {radius:g}mm"
                    "（孔会压在折痕上），请把目标点移离折痕。")
    for k in range(1, 2 ** nh):
        pos = height * k / 2 ** nh
        dist = abs(ty - pos)
        if dist < r:
            return (f"目标孔心 ({tx:.2f}, {ty:.2f}) 距水平折痕 y={pos:.2f}mm 仅 "
                    f"{dist:.2f}mm，小于打孔半径 {radius:g}mm"
                    "（孔会压在折痕上），请把目标点移离折痕。")
    return None


def crease_grid(width, height, nv, nh):
    """计算展开后全部折痕线段（mm，原纸坐标，y 自下而上）。"""
    lines = []
    for k in range(1, 2 ** nv):
        lines.append({"orient": "v", "pos": round(width * k / 2 ** nv, 6)})
    for k in range(1, 2 ** nh):
        lines.append({"orient": "h", "pos": round(height * k / 2 ** nh, 6)})
    return lines


# ---------------------------------------------------------------------------
# 原纸保护区（轴对齐矩形，原纸 mm 坐标）
# ---------------------------------------------------------------------------
def circle_touches_zone(cx, cy, radius, z):
    """孔圆与轴对齐矩形保护区是否相交或相切（含 EDGE_TOL 容差）。

    圆心到矩形的最近距离 <= 半径即相交/相切；保护区为禁入区域，
    相切也算违规。
    """
    dx = max(z["x"] - cx, 0.0, cx - (z["x"] + z["w"]))
    dy = max(z["y"] - cy, 0.0, cy - (z["y"] + z["h"]))
    limit = radius + EDGE_TOL
    return dx * dx + dy * dy <= limit * limit


def zone_out_of_bounds(zones, width, height):
    """返回第一个越出原纸的保护区下标；都在纸内返回 None。"""
    for i, z in enumerate(zones):
        if (z["x"] + z["w"] > width + EDGE_TOL
                or z["y"] + z["h"] > height + EDGE_TOL):
            return i
    return None


def zone_bounds_msg(i, z, width, height):
    return (f"保护区 #{i + 1}（x {z['x']:g} ~ {z['x'] + z['w']:g}mm，"
            f"y {z['y']:g} ~ {z['y'] + z['h']:g}mm）超出原纸 "
            f"{width:g} × {height:g}mm 的范围，请修正或删除该保护区。")


def zone_hit_msg(prefix, hx, hy, zi, z, radius):
    return (f"{prefix}展开后的孔 ({hx:.2f}, {hy:.2f})mm 与保护区 #{zi + 1}"
            f"（x {z['x']:g} ~ {z['x'] + z['w']:g}mm，"
            f"y {z['y']:g} ~ {z['y'] + z['h']:g}mm）相交或相切"
            f"（孔半径 {radius:g}mm，保护区为禁入区域），"
            "请移开打孔点或调整保护区。")


def find_zone_hit(raw_holes, zones, radius, n_images):
    """在展开后的原始孔位（未去重）中找第一个触碰保护区的孔。

    raw_holes 按 unfold_points 的输出顺序排列：第 k 个孔来自第
    k // n_images 个打孔点（每个打孔点恰好展开出 n_images = 2^折数 个
    原始孔位）。返回 (打孔点下标, 孔坐标, 保护区下标) 或 None。
    """
    if not zones or not raw_holes:
        return None
    for k, (x, y) in enumerate(raw_holes):
        src = k // n_images
        for zi, z in enumerate(zones):
            if circle_touches_zone(x, y, radius, z):
                return src, {"x": round(x, 6), "y": round(y, 6)}, zi
    return None


# ---------------------------------------------------------------------------
# 最小纸桥宽度（相邻孔圆边净距下限，0 表示关闭）
# ---------------------------------------------------------------------------
def find_bridge_conflict(uniq_holes, radius, min_bridge):
    """在按坐标容差合并后的孔位中找第一对纸桥净距不足的孔。

    净距 = 圆心距 - 2r（两孔圆边之间的净距离，即"纸桥"宽度）：净距小于
    min_bridge 即冲突，相等可通过。min_bridge <= 0 时关闭检查。

    用空间网格查找邻近孔对：格子边长取判定距离上限 D = 2r + min_bridge，
    净距不足的两孔圆心距必小于 D，必落在相邻九宫格内，判定结果与逐对
    比较一致，但大批量目标排版（数千个孔）不至于平方级耗时。

    uniq_holes 为 (x, y) 列表，按合并（去重）后的顺序扫描，返回首个冲突对
    (孔A下标, 孔B下标, 孔A坐标, 孔B坐标, 实际净距)；无冲突返回 None。
    """
    if min_bridge <= 0.0 or len(uniq_holes) < 2:
        return None
    limit = 2.0 * radius + min_bridge   # 冲突 <=> 圆心距 < limit
    limit_sq = limit * limit
    cell = limit
    cells = {}
    for j, (x, y) in enumerate(uniq_holes):
        cx, cy = math.floor(x / cell), math.floor(y / cell)
        for ix in (cx - 1, cx, cx + 1):
            for iy in (cy - 1, cy, cy + 1):
                for i, (qx, qy) in cells.get((ix, iy), ()):
                    dx, dy = x - qx, y - qy
                    if dx * dx + dy * dy < limit_sq:
                        return (i, j, (qx, qy), (x, y),
                                math.hypot(dx, dy) - 2.0 * radius)
        cells.setdefault((cx, cy), []).append((j, (x, y)))
    return None


def bridge_conflict_msg(prefix, hole_a, hole_b, clearance, min_bridge):
    """纸桥冲突的中文提示：指出两孔坐标、实际净距与设定宽度。"""
    (ax, ay), (bx, by) = hole_a, hole_b
    extra = "（两孔圆已重叠）" if clearance < 0 else ""
    return (f"{prefix}孔 ({ax:.2f}, {ay:.2f})mm 与孔 ({bx:.2f}, {by:.2f})mm "
            f"的圆边净距仅 {clearance:.2f}mm{extra}，小于最小纸桥宽度 "
            f"{min_bridge:g}mm，请调整孔位或关闭/调小该宽度后重新计算。")


def bridge_conflict_info(hole_a, hole_b, clearance):
    """冲突对的结构化信息（坐标与净距保留 6 位小数）。"""
    (ax, ay), (bx, by) = hole_a, hole_b
    return {"holeA": {"x": round(ax, 6), "y": round(ay, 6)},
            "holeB": {"x": round(bx, 6), "y": round(by, 6)},
            "clearance": round(clearance, 6)}


# ---------------------------------------------------------------------------
# SVG 生成
# ---------------------------------------------------------------------------
def _svg_header(w_mm, h_mm):
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{w_mm:.3f}mm" height="{h_mm:.3f}mm" '
        f'viewBox="0 0 {w_mm:.3f} {h_mm:.3f}">\n'
        f'<rect x="0" y="0" width="{w_mm:.3f}" height="{h_mm:.3f}" '
        'fill="#ffffff" stroke="#1f2937" stroke-width="0.35"/>\n'
    )


def _circle(cx, cy, r, extra=""):
    return (f'<circle cx="{cx:.3f}" cy="{cy:.3f}" r="{r:.3f}" '
            f'fill="#dc2626" stroke="#7f1d1d" stroke-width="0.12"{extra}/>\n')


def build_unfolded_svg(width, height, holes, creases, radius, nv, nh, zones):
    """展开图 SVG。y 由数学坐标（自下而上）换算为 SVG 坐标（自上而下）。"""
    w, h, r = float(width), float(height), float(radius)
    parts = [_svg_header(w, h)]
    parts.append(
        f'<desc>纸张 {w:g}x{h:g}mm；垂直对折 {nv} 次、水平对折 {nh} 次；'
        f'保护区 {len(zones)} 个；共 {len(holes)} 个孔</desc>\n')
    for c in creases:
        if c["orient"] == "v":
            parts.append(
                f'<line x1="{c["pos"]:.3f}" y1="0" x2="{c["pos"]:.3f}" y2="{h:.3f}" '
                'stroke="#9ca3af" stroke-width="0.2" stroke-dasharray="2 1.5"/>\n')
        else:
            yy = h - c["pos"]
            parts.append(
                f'<line x1="0" y1="{yy:.3f}" x2="{w:.3f}" y2="{yy:.3f}" '
                'stroke="#9ca3af" stroke-width="0.2" stroke-dasharray="2 1.5"/>\n')
    # 原纸保护区：绿色半透明填充 + 虚线边框，标序号
    for zi, z in enumerate(zones):
        zy_svg = h - (z["y"] + z["h"])
        parts.append(
            f'<rect x="{z["x"]:.3f}" y="{zy_svg:.3f}" '
            f'width="{z["w"]:.3f}" height="{z["h"]:.3f}" '
            'fill="#16a34a" fill-opacity="0.10" stroke="#15803d" '
            'stroke-width="0.4" stroke-dasharray="2.5 1.8"/>\n')
        if z["w"] >= 8.0 and z["h"] >= 4.5:
            parts.append(
                f'<text x="{z["x"] + 1.0:.3f}" y="{zy_svg + 3.4:.3f}" '
                f'font-size="3" fill="#15803d">保{zi + 1}</text>\n')
    for hp in holes:
        parts.append(_circle(hp["x"], h - hp["y"], r))
    parts.append("</svg>\n")
    return "".join(parts)


def build_folded_svg(w, h, edges, points, radius, error_idx):
    """折叠面 SVG，使用 SVG 显示坐标（y 自上而下）。

    纸面在 viewBox 中内缩 pad；客户端靠返回的 pad / vbWidth / vbHeight
    把点击位置换算回折叠面 mm 坐标。
    """
    pad = max(radius * 2.2, 3.0)
    vb_w, vb_h = w + 2 * pad, h + 2 * pad

    def line(x1, y1, x2, y2, fold):
        color, dash, sw = (("#2563eb", "6 4", 0.9) if fold
                           else ("#1f2937", "none", 1.2))
        da = f' stroke-dasharray="{dash}"' if fold else ""
        return (f'<line x1="{x1:.3f}" y1="{y1:.3f}" x2="{x2:.3f}" y2="{y2:.3f}" '
                f'stroke="{color}" stroke-width="{sw}"{da} stroke-linecap="round"/>\n')

    y_top, y_bot = pad, pad + h
    parts = [
        f'<svg id="folded-svg" viewBox="0 0 {vb_w:.3f} {vb_h:.3f}" '
        'preserveAspectRatio="xMidYMid meet" role="img" '
        'aria-label="折叠面，点击打孔">\n',
        # 点击层（覆盖整个 viewBox）
        f'<rect x="0" y="0" width="{vb_w:.3f}" height="{vb_h:.3f}" fill="transparent"/>\n',
        f'<rect x="{pad:.3f}" y="{pad:.3f}" width="{w:.3f}" height="{h:.3f}" '
        'fill="#fdf6e3" stroke="none"/>\n',
        line(pad, y_bot, pad + w, y_bot, edges["y0"] == "fold"),   # 下边
        line(pad, y_top, pad + w, y_top, edges["y1"] == "fold"),   # 上边
        line(pad, y_top, pad, y_bot, edges["x0"] == "fold"),       # 左边
        line(pad + w, y_top, pad + w, y_bot, edges["x1"] == "fold"),  # 右边
        # 安全区域：孔心进入此绿虚线框内才不会碰边/折痕
        f'<rect x="{pad + radius:.3f}" y="{pad + radius:.3f}" '
        f'width="{max(w - 2 * radius, 0):.3f}" height="{max(h - 2 * radius, 0):.3f}" '
        'fill="none" stroke="#16a34a" stroke-width="0.3" '
        'stroke-dasharray="1.5 1.5" opacity="0.55"/>\n',
    ]
    for i, p in enumerate(points):
        extra = ' data-hole="1"'
        if i == error_idx:
            extra += ' fill="#f59e0b"'
        # 数学坐标 y（自下而上）-> 显示坐标（自上而下）
        parts.append(_circle(pad + p["x"], pad + h - p["y"], radius, extra))
    parts.append("</svg>\n")
    return "".join(parts), pad, vb_w, vb_h


# ---------------------------------------------------------------------------
# A4 纵向分页定位模板（实际 mm 1:1，不缩放）
# ---------------------------------------------------------------------------
def a4_windows(width, height):
    """按原纸尺寸计算覆盖全纸的分页窗口（原纸 mm 坐标，原点左下角）。

    纵向 A4 页四边留白 A4_MARGIN，每页可承载窗口 A4_STEP×277mm；相邻窗口
    步进 = 内容跨度 - A4_OVERLAP，最后一个窗口吸附到纸张另一边，保证末端
    实际重叠恰为 15mm 且窗口不越出纸张。返回
    {cols, rows, pages: [{col, row, x0, y0, x1, y1, overlap:{...}}]}，
    行列号均从 1 起，第 1 行对应原纸最下一行（与左下角坐标方向一致）。
    """
    def starts(size, span, step):
        # 一张纸放得下时不分页（窗口吸附到 0，末端留白属正常）
        if size <= span + 1e-9:
            return [0.0]
        # 步进 step = 内容跨度 - 重叠；n 个窗口覆盖长度 = span + (n-1)*step
        n = 1 + max(1, math.ceil((size - span - 1e-9) / step))
        xs = [i * step for i in range(n - 1)]
        last = size - span
        if not xs:
            xs.append(last)
        elif last <= xs[-1]:
            # 末窗与前窗重叠不足时退化为吸附（极小尺寸，理论上极少触发）
            xs[-1] = last
        else:
            xs.append(last)
        return xs

    span_x = A4_W - 2 * A4_MARGIN
    span_y = A4_H - 2 * A4_MARGIN
    xs = starts(float(width), span_x, A4_STEP_X)
    ys = starts(float(height), span_y, A4_STEP_Y)
    pages = []
    for ri, y0 in enumerate(ys):
        for ci, x0 in enumerate(xs):
            x1 = min(x0 + span_x, float(width))
            y1 = min(y0 + span_y, float(height))
            pages.append({"col": ci + 1, "row": ri + 1,
                          "x0": round(x0, 6), "y0": round(y0, 6),
                          "x1": round(x1, 6), "y1": round(y1, 6)})
    return {"cols": len(xs), "rows": len(ys),
            "pages": pages, "spanX": span_x, "spanY": span_y}


def _mark_positions(lo, hi, gap=60.0, inset=22.0):
    """重叠区内对位十字的坐标位置（沿拼缝方向）。

    距两端 inset 处各一枚，中间每隔 gap 一枚；带段过短时在中点保留一枚，
    保证任何重叠片段上都有可对位标记。坐标以原纸 mm 计（翻转组内坐标）。
    """
    if hi - lo < 2 * inset + gap * 0.5:
        return [round((lo + hi) / 2.0, 3)]
    pos = [lo + inset]
    v = lo + inset + gap
    while v < hi - inset - 1e-9:
        pos.append(round(v, 3))
        v += gap
    pos.append(round(hi - inset, 3))
    return pos


def _seg_rect(ax, ay, bx, by, x0, y0, x1, y1):
    """Liang–Barsky 线段与轴对齐窗口求交；返回窗口内线段端点或 None。"""
    t0, t1, dx, dy = 0.0, 1.0, bx - ax, by - ay
    for p, q in ((-dx, ax - x0), (dx, x1 - ax),
                 (-dy, ay - y0), (dy, y1 - ay)):
        if abs(p) < 1e-12:
            if q < 0.0:
                return None
        else:
            r = q / p
            if p < 0.0:
                if r > t1:
                    return None
                t0 = max(t0, r)
            else:
                if r < t0:
                    return None
                t1 = min(t1, r)
    return ax + t0 * dx, ay + t0 * dy, ax + t1 * dx, ay + t1 * dy


def _circle_clipped_page_path(cx, cy, r, x0, y0, x1, y1, gx, gy):
    """圆与轴对齐窗口求交，返回页坐标系下的闭合填充路径（或 None）。

    做法：圆盘 ∩ 矩形是凸集。沿圆周采样「圆与四条边的交点」角度，再补入
    落在圆内的窗口角点（角点角），按数学角排序后顺时针遍历——相邻事件间
    的圆弧中点在窗口内就走圆弧，否则走直线（直线只会沿同一条窗口边：跨
    边之间必然隔着圆内的角点事件）。圆完全在窗内给整圆、完全包含窗口给
    矩形。输出坐标为页内 mm：px = gx + X - x0，py = gy + (y1 - Y)。
    """
    TWO_PI = 2.0 * math.pi
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]

    def page(x, y):
        return gx + x - x0, gy + (y1 - y)

    # 圆完全位于窗口内 -> 整圆（页坐标下两段半圆弧，sweep=0 为顺时针）
    if cx - r >= x0 - 1e-9 and cx + r <= x1 + 1e-9 \
            and cy - r >= y0 - 1e-9 and cy + r <= y1 + 1e-9:
        acx, acy = page(cx, cy)
        return (f"M {acx - r:.3f},{acy:.3f} "
                f"a {r:.3f},{r:.3f} 0 1,0 {2 * r:.3f},0 "
                f"a {r:.3f},{r:.3f} 0 1,0 {-2 * r:.3f},0 Z")

    # 圆完全包含窗口 -> 交集就是窗口矩形
    if all(math.hypot(qx - cx, qy - cy) <= r + 1e-9 for qx, qy in corners):
        ax, ay = page(x0, y1)
        return (f"M {ax:.3f},{ay:.3f} h {x1 - x0:.3f} v {y1 - y0:.3f} "
                f"h {-(x1 - x0):.3f} Z")

    angles = []

    def add_angle(a, qx, qy):
        # 用真实交点坐标重算角度，避免边界相切时 asin/acos 漂移
        a = math.atan2(qy - cy, qx - cx) % TWO_PI
        for ea, _, _ in angles:
            d = abs(a - ea) % TWO_PI
            if min(d, TWO_PI - d) < 1e-7:
                return
        angles.append((a, qx, qy))

    # 圆与左/右边界直线（保留线段范围内的交点）
    for bx in (x0, x1):
        rem = r * r - (bx - cx) ** 2
        if rem >= 0.0:
            k = math.sqrt(max(0.0, rem))
            for qy in (cy + k, cy - k):
                if y0 - 1e-9 <= qy <= y1 + 1e-9:
                    add_angle(math.atan2(qy - cy, bx - cx),
                              bx, min(y1, max(y0, qy)))
    # 圆与下/上边界直线
    for by in (y0, y1):
        rem = r * r - (by - cy) ** 2
        if rem >= 0.0:
            k = math.sqrt(max(0.0, rem))
            for qx in (cx + k, cx - k):
                if x0 - 1e-9 <= qx <= x1 + 1e-9:
                    add_angle(math.atan2(by - cy, qx - cx),
                              min(x1, max(x0, qx)), by)

    # 落在圆内的窗口角点（严格在内）：作为直线边界的转折点
    for qx, qy in corners:
        if math.hypot(qx - cx, qy - cy) < r - 1e-7:
            add_angle(math.atan2(qy - cy, qx - cx), qx, qy)

    angles.sort(key=lambda e: e[0])
    if len(angles) < 2:
        return None

    def in_win(x, y):
        return x0 - 1e-7 <= x <= x1 + 1e-7 and y0 - 1e-7 <= y <= y1 + 1e-7

    d = []
    n = len(angles)
    for i in range(n):
        a1, sx, sy = angles[i]
        a2, ex, ey = angles[(i + 1) % n]
        if a2 <= a1:
            a2 += TWO_PI
        am = (a1 + a2) / 2.0
        mx = cx + r * math.cos(am)
        my = cy + r * math.sin(am)
        psx, psy = page(sx, sy)
        pex, pey = page(ex, ey)
        if i == 0:
            d.append(f"M {psx:.3f},{psy:.3f}")
        if in_win(mx, my):
            # 圆内弧：页坐标 y 已翻转，顺时针遍历对应 sweep-flag = 1
            large = 1 if a2 - a1 > math.pi else 0
            d.append(f"A {r:.3f},{r:.3f} 0 {large},1 {pex:.3f},{pey:.3f}")
        else:
            # 窗外段沿窗口边直线（相邻事件必在同一条边上，角点已在列表中）
            d.append(f"L {pex:.3f},{pey:.3f}")
    d.append("Z")
    return " ".join(d)


def build_a4_page_svg(page, paging, width, height, holes, creases, radius,
                      nv, nh, zones, seq, total):
    """单张纵向 A4 分页 SVG（实际 mm 1:1）。

    页面 210×297mm、四边留白 10mm；原纸 mm 坐标（原点左下角、y 向上）到
    页内 SVG 坐标（原点左上角、y 向下）直接换算：
        px = MARGIN + (X - x0)， py = MARGIN + (y1 - Y)
    所有图形在服务端预先与本页窗口求交（线段裁切、圆裁为圆弧+窗口边的
    闭合路径、矩形求交），输出不依赖 clipPath/transform，保证浏览器、
    打印与常见 SVG 渲染器结果一致；跨页图形在相邻页各留窗口内片段。
    重叠区做浅色底纹，在拼缝两侧页同一原纸坐标各画一枚对位十字；
    标注文字始终正向可读。
    """
    span_x = paging["spanX"]
    span_y = paging["spanY"]
    x0, y0, x1, y1 = page["x0"], page["y0"], page["x1"], page["y1"]
    cw, ch = x1 - x0, y1 - y0                       # 窗口映射到页内的宽高
    gx, gy = A4_MARGIN, A4_MARGIN                   # 内容框在页上的位置
    col, row = page["col"], page["row"]

    # 原纸 mm -> 页内 SVG mm（y 翻转）
    def px(x):
        return gx + x - x0

    def py(y):
        return gy + (y1 - y)

    # 与四邻窗口的实际重叠区间（原纸 mm）：末窗吸附会使相邻窗口重叠大于
    # 15mm，对位十字与底纹必须按两窗真实相交区域绘制，且相邻页用同一组
    # 原始数据计算，坐标天然一致、可重合对位。
    by_pos = {(p["col"], p["row"]): p for p in paging["pages"]}
    seams = []
    left = by_pos.get((col - 1, row))
    if left:
        seams.append(("v", (x0, left["x1"]), (y0, y1)))
    right = by_pos.get((col + 1, row))
    if right:
        seams.append(("v", (right["x0"], x1), (y0, y1)))
    below = by_pos.get((col, row - 1))
    if below:
        seams.append(("h", (y0, below["y1"]), (x0, x1)))
    above = by_pos.get((col, row + 1))
    if above:
        seams.append(("h", (above["y0"], y1), (x0, x1)))

    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{A4_W:.3f}mm" height="{A4_H:.3f}mm" '
        f'viewBox="0 0 {A4_W:.3f} {A4_H:.3f}">\n'
        '<title>A4 分页定位模板 '
        f'第 {seq} 页（共 {total} 页），列 {col}/{paging["cols"]}，'
        f'行 {row}/{paging["rows"]}，自下而上</title>\n'
        # 整页白底（打印时不会透出底色）
        f'<rect x="0" y="0" width="{A4_W:.3f}" height="{A4_H:.3f}" '
        'fill="#ffffff"/>\n'
        # 页内可打印区域参考框
        f'<rect x="{A4_MARGIN:.3f}" y="{A4_MARGIN:.3f}" '
        f'width="{A4_W - 2 * A4_MARGIN:.3f}" '
        f'height="{A4_H - 2 * A4_MARGIN:.3f}" '
        'fill="none" stroke="#e2e8f0" stroke-width="0.15"/>\n'
        # 窗口内纸面底色（仅本页窗口大小，即纸边范围内）
        f'<rect x="{gx:.3f}" y="{gy:.3f}" width="{cw:.3f}" height="{ch:.3f}" '
        'fill="#fdfdfb"/>\n'
    ]

    # 重叠区浅色底纹：左右拼缝（淡蓝）、上下拼缝（淡绿），按实际相交区间
    # 绘制。用显式浅色而非 fill-opacity，避免不同打印/渲染通道对透明度的
    # 处理差异；底色很淡，不影响孔圆、折痕等图形判读
    for kind, s_range, t_range in seams:
        s_lo, s_hi = s_range
        t_lo, t_hi = t_range
        color = "#dbeafe" if kind == "v" else "#dcfce7"
        edge = "#93c5fd" if kind == "v" else "#86efac"
        if kind == "v":
            parts.append(
                f'<rect x="{px(s_lo):.3f}" y="{py(t_hi):.3f}" '
                f'width="{s_hi - s_lo:.3f}" height="{t_hi - t_lo:.3f}" '
                f'fill="{color}" stroke="{edge}" stroke-width="0.12" '
                'stroke-dasharray="1.2 1.2"/>\n')
        else:
            parts.append(
                f'<rect x="{px(t_lo):.3f}" y="{py(s_hi):.3f}" '
                f'width="{t_hi - t_lo:.3f}" height="{s_hi - s_lo:.3f}" '
                f'fill="{color}" stroke="{edge}" stroke-width="0.12" '
                'stroke-dasharray="1.2 1.2"/>\n')

    # 折痕（灰虚线）：与窗口求交后绘制，跨页折痕在相邻页保留对应片段
    for c in creases:
        if c["orient"] == "v":
            seg = _seg_rect(c["pos"], y0, c["pos"], y1, x0, y0, x1, y1)
            if seg:
                ax, ay, bx, by_ = seg
                parts.append(
                    f'<line x1="{px(ax):.3f}" y1="{py(ay):.3f}" '
                    f'x2="{px(bx):.3f}" y2="{py(by_):.3f}" '
                    'stroke="#9ca3af" stroke-width="0.2" '
                    'stroke-dasharray="2 1.5"/>\n')
        else:
            seg = _seg_rect(x0, c["pos"], x1, c["pos"], x0, y0, x1, y1)
            if seg:
                ax, ay, bx, by_ = seg
                parts.append(
                    f'<line x1="{px(ax):.3f}" y1="{py(ay):.3f}" '
                    f'x2="{px(bx):.3f}" y2="{py(by_):.3f}" '
                    'stroke="#9ca3af" stroke-width="0.2" '
                    'stroke-dasharray="2 1.5"/>\n')

    # 保护区（绿色半透明 + 虚线框）：与窗口求交；序号按可见片段大小标注
    for zi, z in enumerate(zones):
        zx0, zy0 = max(z["x"], x0), max(z["y"], y0)
        zx1, zy1 = min(z["x"] + z["w"], x1), min(z["y"] + z["h"], y1)
        if zx1 - zx0 <= 0 or zy1 - zy0 <= 0:
            continue
        parts.append(
            f'<rect x="{px(zx0):.3f}" y="{py(zy1):.3f}" '
            f'width="{zx1 - zx0:.3f}" height="{zy1 - zy0:.3f}" '
            'fill="#dcfce7" stroke="#15803d" '
            'stroke-width="0.4" stroke-dasharray="2.5 1.8"/>\n')
        if zx1 - zx0 >= 8.0 and zy1 - zy0 >= 4.5:
            parts.append(
                f'<text x="{px(zx0) + 1.0:.3f}" y="{py(zy1) + 3.4:.3f}" '
                f'font-size="3" fill="#15803d">保{zi + 1}</text>\n')

    # 孔圆（红）：预先与窗口求交，跨页时绘制圆与窗口相交的片段路径
    for hp in holes:
        d = _circle_clipped_page_path(
            hp["x"], hp["y"], radius, x0, y0, x1, y1, gx, gy)
        if d:
            parts.append(
                f'<path d="{d}" fill="#dc2626" stroke="#7f1d1d" '
                'stroke-width="0.12"/>\n')

    # 对位十字（紫，位于重叠带中线）：相邻页同原纸坐标各画一份，打印后重合
    for kind, s_range, t_range in seams:
        s_lo, s_hi = s_range
        sc = (s_lo + s_hi) / 2.0
        for m in _mark_positions(t_range[0], t_range[1]):
            if kind == "v":
                cx, cy = px(sc), py(m)
            else:
                cx, cy = px(m), py(sc)
            parts.append(
                f'<line x1="{cx - 2.5:.3f}" y1="{cy:.3f}" '
                f'x2="{cx + 2.5:.3f}" y2="{cy:.3f}" '
                'stroke="#7c3aed" stroke-width="0.35"/>\n'
                f'<line x1="{cx:.3f}" y1="{cy - 2.5:.3f}" '
                f'x2="{cx:.3f}" y2="{cy + 2.5:.3f}" '
                'stroke="#7c3aed" stroke-width="0.35"/>\n'
                f'<circle cx="{cx:.3f}" cy="{cy:.3f}" r="0.7" '
                'fill="none" stroke="#7c3aed" stroke-width="0.25"/>\n')

    # ---- 窗口边界黑框（= 本页原纸窗口四边 / 纸边片段）----
    parts.append(
        f'<rect x="{gx:.3f}" y="{gy:.3f}" width="{cw:.3f}" height="{ch:.3f}" '
        'fill="none" stroke="#1f2937" stroke-width="0.35"/>\n')

    # ---- 顶部留白：页号 / 行列号 ----
    parts.append(
        f'<text x="{A4_MARGIN:.3f}" y="6.5" font-size="4.2" '
        'font-weight="bold" fill="#0f172a">'
        f'A4 分页定位模板 · 第 {seq} / {total} 页'
        f'（列 {col}/{paging["cols"]}，行 {row}/{paging["rows"]}，'
        '行号自下而上）</text>\n')

    # ---- 底部留白：原纸窗口坐标（统一左下角原点，y 向上）----
    foot_y = A4_H - 2.2
    parts.append(
        f'<text x="{A4_MARGIN:.3f}" y="{foot_y:.3f}" font-size="3.2" '
        f'fill="#475569">原纸 {width:g}×{height:g}mm（原点左下角，y 向上）；'
        f'本页窗口 x {x0:g}~{x1:g}mm，y {y0:g}~{y1:g}mm；'
        f'1:1 实际毫米，禁止缩放</text>\n'
        f'<text x="{A4_MARGIN:.3f}" y="{foot_y - 4.2:.3f}" font-size="3" '
        f'fill="#64748b">窗口左下角 ({x0:g}, {y0:g})mm，'
        f'右上角 ({x1:g}, {y1:g})mm；四边留白 {A4_MARGIN:g}mm，'
        f'标准相邻页重叠 {A4_OVERLAP:g}mm（末窗吸附时底纹可能更宽）；'
        '紫十字为重叠区对位标记，两页同坐标十字重合即对齐</text>\n')

    # 第 1 页（覆盖原纸左下角）：标出坐标方向与原点（箭头朝 x、y 正向）。
    # 从角点向内 8mm 起画，整组标记落在纸内，不压窗口黑框。
    if col == 1 and row == 1:
        ox, oy = gx + 8.0, gy + ch - 8.0
        parts.append(
            '<defs><marker id="arr" markerWidth="5" markerHeight="5" '
            'refX="4" refY="2.5" orient="auto" markerUnits="strokeWidth">'
            '<path d="M0,0 L5,2.5 L0,5 Z" fill="#dc2626"/></marker></defs>\n'
            f'<line x1="{ox:.3f}" y1="{oy:.3f}" x2="{ox + 11:.3f}" '
            f'y2="{oy:.3f}" stroke="#dc2626" stroke-width="0.4" '
            'marker-end="url(#arr)"/>\n'
            f'<line x1="{ox:.3f}" y1="{oy:.3f}" x2="{ox:.3f}" '
            f'y2="{oy - 11:.3f}" stroke="#dc2626" stroke-width="0.4" '
            'marker-end="url(#arr)"/>\n'
            f'<text x="{ox + 12.5:.3f}" y="{oy + 1.2:.3f}" font-size="3.2" '
            'fill="#dc2626">x</text>\n'
            f'<text x="{ox - 1.2:.3f}" y="{oy - 12.5:.3f}" font-size="3.2" '
            'fill="#dc2626">y↑</text>\n'
            f'<text x="{ox - 1.0:.3f}" y="{oy + 4.0:.3f}" font-size="3" '
            'fill="#dc2626">O(0,0)</text>\n')

    parts.append('</svg>\n')
    return "".join(parts)


def build_a4_readme(width, height, radius, paging, holes_count):
    """ZIP 内随附的打印拼接说明（UTF-8 文本）。"""
    cols, rows = paging["cols"], paging["rows"]
    lines = [
        "A4 分页定位模板 —— 打印与拼接说明",
        "=" * 40,
        "",
        f"原纸尺寸：{width:g} × {height:g} mm（坐标原点在原纸左下角，y 轴向上）",
        f"打孔半径：{radius:g} mm；展开后孔位（重合已合并）：{holes_count} 个",
        f"分页：共 {len(paging['pages'])} 页 = {cols} 列 × {rows} 行",
        "（列号从左到右、行号从下到上，第 1 行对应原纸最下一行）",
        f"页面：纵向 A4 {A4_W:g}×{A4_H:g}mm，四边留白 {A4_MARGIN:g}mm，"
        f"相邻页标准重叠 {A4_OVERLAP:g}mm",
        "比例：图形按实际毫米 1:1 绘制，未做任何缩放。",
        "",
        "分页规则：窗口从原纸左下角起步，x 向每 175mm、y 向每 262mm 步进",
        "排列，最后一个窗口吸附到纸张另一边（因此末端相邻页实际重叠可能",
        "大于 15mm，以浅色重叠带与紫色对位十字为准，对齐方法不变）。",
        "",
        "打印要求（务必遵守）",
        "-" * 40,
        "1. 使用 A4 纵向纸张单面打印。",
        "2. 打印缩放必须选择【100% / 实际大小 / 无缩放】，",
        "   切勿选择“适合页面 / Fit to page”（否则 1:1 尺寸失效）。",
        "3. 打印后用尺核对：任意一页的纸张窗口黑框宽度应为 "
        f"{A4_W - 2 * A4_MARGIN:g}mm（不足一页时按实际窗口宽）、"
        f"高度最大 {A4_H - 2 * A4_MARGIN:g}mm；",
        "   或核对页内任一孔圆直径/标注尺寸与设定值一致。",
        "",
        "拼接方法",
        "-" * 40,
        "1. 每页页眉标有页号与行列号，页脚标有本页在原纸上的窗口坐标",
        "   （x、y 均以原纸左下角为原点，单位 mm）。",
        "2. 按列从左到右、行从下到上排列页面（文件名中的 r01 即最下一行）。",
        "3. 相邻页有 15mm 重叠区（蓝/绿浅色带），带内紫色对位十字在两页上",
        "   对应同一原纸坐标：对齐时让两页的紫色十字完全重合，窗口黑框",
        "   边线自然落在另一页窗口内 15mm 处，固定后裁掉多余部分即可。",
        "4. 跨页图形（孔圆、折痕、保护区、纸边）在相邻页各保留窗口内片段，",
        "   按对位十字拼合后即恢复完整图形，无需也不能拉伸缩放。",
        "5. 标有 O(0,0) 的一页为原纸左下角页，红箭头分别指向 x、y 正向。",
        "",
        "文件清单",
        "-" * 40,
        "a4-page-NN-r行-c列.svg：按页号排序（行优先，行内从左到右）。",
    ]
    for i, p in enumerate(paging["pages"], 1):
        lines.append(
            f"  第 {i:02d} 页 r{p['row']:02d}c{p['col']:02d}："
            f"窗口 x {p['x0']:g}~{p['x1']:g}mm，"
            f"y {p['y0']:g}~{p['y1']:g}mm")
    lines.append("README.txt：本说明。")
    lines.append("")
    return "\n".join(lines)


def build_a4_zip(result, width, height, radius, nv, nh, zones):
    """把合法展开结果打包为 A4 分页 SVG 的 ZIP（内存字节）。"""
    holes = result["holes"]
    creases = crease_grid(width, height, nv, nh)
    paging = a4_windows(width, height)
    if len(paging["pages"]) > A4_MAX_PAGES:
        raise ValueError(
            f"原纸 {width:g}×{height:g}mm 需要 {len(paging['pages'])} 张 A4，"
            f"超过分页上限 {A4_MAX_PAGES} 张，无法生成 A4 模板。")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        total = len(paging["pages"])
        for i, page in enumerate(paging["pages"], 1):
            svg = build_a4_page_svg(page, paging, width, height, holes, creases,
                                    radius, nv, nh, zones, i, total)
            name = (f"a4-page-{i:02d}-r{page['row']:02d}c{page['col']:02d}.svg")
            zf.writestr(name, svg)
        zf.writestr("README.txt", build_a4_readme(
            width, height, radius, paging, len(holes)))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 请求处理
# ---------------------------------------------------------------------------
def _bad(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def _num(obj, key, lo, hi, name):
    val = obj.get(key)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"{name}必须是数字")
    val = float(val)
    if not math.isfinite(val) or val < lo or val > hi:
        raise ValueError(f"{name}须在 {lo:g} ~ {hi:g} 之间")
    return val


def _validate_sheet(payload):
    """校验纸张尺寸、打孔半径、折法与最小纸桥宽度。

    返回 (width, height, radius, folds, min_bridge)；minBridge 缺省为 0
    （关闭纸桥检查）。
    """
    width = _num(payload, "width", MIN_DIM, MAX_DIM, "纸张宽度")
    height = _num(payload, "height", MIN_DIM, MAX_DIM, "纸张高度")
    radius = _num(payload, "radius", MIN_RADIUS, MAX_RADIUS, "打孔半径")
    min_bridge = _num(payload, "minBridge", MIN_BRIDGE, MAX_BRIDGE,
                      "最小纸桥宽度") if payload.get("minBridge") is not None else 0.0

    folds = payload.get("folds", [])
    if not isinstance(folds, list) or len(folds) > MAX_FOLDS:
        raise ValueError(f"折叠次数须为 0 ~ {MAX_FOLDS} 次")
    for f in folds:
        if f not in ALLOWED_FOLDS:
            raise ValueError("存在非法折叠方向，可选：vL / vR / hT / hB")
    return width, height, radius, folds, min_bridge


def _validate_norm_points(payload):
    """校验折叠面归一化打孔点列表，返回 [{"x","y"} 归一化]。"""
    points = payload.get("points", [])
    if not isinstance(points, list) or len(points) > MAX_HOLES:
        raise ValueError(f"打孔点数量须为 0 ~ {MAX_HOLES} 个")
    norm_pts = []
    for p in points:
        if not isinstance(p, dict):
            raise ValueError("打孔点格式错误")
        norm_pts.append({"x": _num(p, "x", 0.0, 1.0, "打孔点 x"),
                         "y": _num(p, "y", 0.0, 1.0, "打孔点 y")})
    return norm_pts


def _check_face_capacity(w, h, radius):
    """折叠面必须放得下打孔器（直径 + 容差）。"""
    if w < 2 * radius + 2 * EDGE_TOL or h < 2 * radius + 2 * EDGE_TOL:
        raise ValueError(
            f"折叠后纸面为 {w:g} × {h:g}mm，放不下直径 {2 * radius:g}mm 的打孔器，"
            "请减少折叠次数或减小打孔半径。")


def _validate_zones(payload):
    """校验保护区结构：至多 MAX_ZONES 个轴对齐矩形 {x, y, w, h}（原纸 mm）。

    只校验格式与取值范围；是否越出原纸由调用方结合纸张尺寸判定
    （compute 按孔位错误同款方式返回 200 + error，reverse 抛 ValueError）。
    """
    zones = payload.get("zones", [])
    if not isinstance(zones, list) or len(zones) > MAX_ZONES:
        raise ValueError(f"保护区数量须为 0 ~ {MAX_ZONES} 个")
    out = []
    for i, z in enumerate(zones):
        if not isinstance(z, dict):
            raise ValueError("保护区格式错误")
        zx = _num(z, "x", 0.0, MAX_DIM, f"保护区 #{i + 1} 的 x")
        zy = _num(z, "y", 0.0, MAX_DIM, f"保护区 #{i + 1} 的 y")
        zw = _num(z, "w", 0.0, MAX_DIM, f"保护区 #{i + 1} 的宽")
        zh = _num(z, "h", 0.0, MAX_DIM, f"保护区 #{i + 1} 的高")
        if zw <= 0.0 or zh <= 0.0:
            raise ValueError(f"保护区 #{i + 1} 的宽和高必须大于 0")
        out.append({"x": zx, "y": zy, "w": zw, "h": zh})
    return out


def compute(payload):
    """核心计算。成功返回结果 dict，失败抛出 ValueError。"""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")

    width, height, radius, folds, min_bridge = _validate_sheet(payload)
    zones = _validate_zones(payload)
    norm_pts = _validate_norm_points(payload)

    w, h, edges, nv, nh = folded_state(width, height, folds)
    _check_face_capacity(w, h, radius)

    # 客户端坐标是归一化的；换算为折叠面 mm 坐标
    mm_pts = [{"x": p["x"] * w, "y": p["y"] * h} for p in norm_pts]

    error_msg, error_idx = None, -1
    zone_error = None
    bridge_error = None
    holes, creases, unfolded_svg = [], [], None

    # 保护区必须完整位于原纸内（纸张尺寸改小导致越界时在此提示修正）
    oob = zone_out_of_bounds(zones, width, height)
    if oob is not None:
        error_msg = zone_bounds_msg(oob, zones[oob], width, height)
        zone_error = {"kind": "bounds", "zoneIndex": oob}

    if error_msg is None:
        for i, p in enumerate(mm_pts):
            err = check_point(p["x"], p["y"], w, h, radius, edges)
            if err:
                error_msg, error_idx = err, i
                break

    n_images = 2 ** len(folds)
    if error_msg is None:
        raw = unfold_points(mm_pts, folds, width, height)
        # 任一展开孔圆与任一保护区相交/相切：指出原打孔点、展开孔与保护区
        hit = find_zone_hit(raw, zones, radius, n_images)
        if hit:
            src, hole, zi = hit
            p = mm_pts[src]
            error_msg, error_idx = zone_hit_msg(
                f"打孔点 #{src + 1}（折叠面 ({p['x']:.2f}, {p['y']:.2f})mm）",
                hole["x"], hole["y"], zi, zones[zi], radius), src
            zone_error = {"kind": "hit", "pointIndex": src,
                          "hole": hole, "zoneIndex": zi}
        else:
            # 先按现有坐标容差合并展开孔位，再检查任意两个不同孔的圆边净距；
            # 冲突时不生成展开图、禁止下载
            uniq, raw_of = dedupe_with_index(raw)
            conflict = find_bridge_conflict(uniq, radius, min_bridge)
            if conflict:
                ia, ib, ha, hb, clearance = conflict
                # 合并后的孔来自哪个原打孔点（raw 下标 // n_images）
                first_raw = {}
                for k, ui in enumerate(raw_of):
                    first_raw.setdefault(ui, k)
                src = first_raw[ia] // n_images
                p = mm_pts[src]
                error_msg, error_idx = bridge_conflict_msg(
                    f"打孔点 #{src + 1}（折叠面 ({p['x']:.2f}, {p['y']:.2f})mm）"
                    "展开后的", ha, hb, clearance, min_bridge), src
                bridge_error = bridge_conflict_info(ha, hb, clearance)
            else:
                holes = [{"x": round(x, 6), "y": round(y, 6)} for x, y in uniq]
                creases = crease_grid(width, height, nv, nh)
                unfolded_svg = build_unfolded_svg(
                    width, height, holes, creases, radius, nv, nh, zones)

    folded_svg, pad, vb_w, vb_h = build_folded_svg(
        w, h, edges, mm_pts, radius, error_idx)

    # A4 分页窗口只依赖原纸尺寸（折叠面校验通过即可计算）；页数供页面展示，
    # 模板 ZIP 仅在全部孔位/保护区/纸桥校验通过（error 为空）时才允许下载
    a4_paging = a4_windows(width, height)
    a4_info = {
        "pageCount": len(a4_paging["pages"]),
        "cols": a4_paging["cols"], "rows": a4_paging["rows"],
        "pageW": A4_W, "pageH": A4_H,
        "margin": A4_MARGIN, "overlap": A4_OVERLAP,
        "pages": a4_paging["pages"],
    }

    return {
        "ok": True,
        "folded": {
            "width": round(w, 6), "height": round(h, 6),
            "layers": 2 ** len(folds),
            "edges": edges,
            "pad": round(pad, 6),
            "vbWidth": round(vb_w, 6), "vbHeight": round(vb_h, 6),
            "svg": folded_svg,
        },
        "creases": creases,
        "holes": holes,
        "holeCount": len(holes),
        "expectedCount": len(mm_pts) * (2 ** len(folds)),
        "unfoldedSvg": unfolded_svg,
        "a4Paging": a4_info,
        "error": error_msg,
        "errorIndex": error_idx,
        "zoneError": zone_error,
        "bridgeError": bridge_error,
    }


def reverse_lookup(payload):
    """反查打孔：原纸上的目标孔心 -> 最终折叠面的归一化孔心 + 全部连带孔位。

    目标点或折回后的孔心若使打孔半径触及纸张边界/折痕，抛出带具体原因的
    ValueError，客户端据此禁止确认。连带孔与保护区相交/相切时同样拒绝。
    纸桥宽度按确认加入现有打孔点后的全集判定：现有孔与本次连带孔一并展开、
    按坐标容差合并后检查圆边净距，存在冲突时无法确认。
    """
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")

    width, height, radius, folds, min_bridge = _validate_sheet(payload)
    zones = _validate_zones(payload)
    norm_pts = _validate_norm_points(payload)

    target = payload.get("target")
    if not isinstance(target, dict):
        raise ValueError("缺少目标孔心 target（原纸 mm 坐标）")
    tx = _num(target, "x", 0.0, width, "目标孔心 x")
    ty = _num(target, "y", 0.0, height, "目标孔心 y")

    w, h, edges, nv, nh = folded_state(width, height, folds)
    _check_face_capacity(w, h, radius)

    # 保护区必须完整位于原纸内
    oob = zone_out_of_bounds(zones, width, height)
    if oob is not None:
        raise ValueError(zone_bounds_msg(oob, zones[oob], width, height))

    # 目标孔心在原纸上的校验（边界 + 全部折痕）
    err = check_target_on_paper(tx, ty, width, height, radius, nv, nh)
    if err:
        raise ValueError(err)

    # 按当前折叠顺序及折向逐层折回最终折叠面
    fx, fy = fold_point_to_face(tx, ty, folds, width, height)

    # 反查孔心在折叠面上的校验（与上面的校验几何等价，作浮点兜底）
    err = check_point(fx, fy, w, h, radius, edges)
    if err:
        raise ValueError(f"反查到折叠面的{err}")

    # 复用现有展开算法，列出这一次打孔必然生成的全部孔位
    raw = unfold_points([{"x": fx, "y": fy}], folds, width, height)

    # 与折叠面打孔同一判定：任一连带孔圆触碰保护区即拒绝
    hit = find_zone_hit(raw, zones, radius, 2 ** len(folds))
    if hit:
        _, hole, zi = hit
        raise ValueError(zone_hit_msg(
            f"反查打孔（目标孔心 ({tx:.2f}, {ty:.2f})mm）",
            hole["x"], hole["y"], zi, zones[zi], radius))

    holes = dedupe_points(raw)

    # 最小纸桥宽度：按确认加入现有打孔点后的全集判定。现有孔与本次连带孔
    # 一并展开、按坐标容差合并后检查任意两孔圆边净距（相等可通过）。
    if min_bridge > 0.0:
        existing = unfold_points(
            [{"x": p["x"] * w, "y": p["y"] * h} for p in norm_pts],
            folds, width, height)
        uniq, _idx = dedupe_with_index(existing + raw)
        conflict = find_bridge_conflict(uniq, radius, min_bridge)
        if conflict:
            ia, ib, ha, hb, clearance = conflict
            n_exist_uniq = len(dedupe_with_index(existing)[0])
            if ia >= n_exist_uniq and ib >= n_exist_uniq:
                prefix = "本次打孔的连带孔中，"
            elif ia < n_exist_uniq and ib < n_exist_uniq:
                prefix = "现有孔位中，"
            else:
                prefix = (f"反查打孔（目标孔心 ({tx:.2f}, {ty:.2f})mm）"
                          "与现有孔冲突：")
            raise ValueError(
                bridge_conflict_msg(prefix, ha, hb, clearance, min_bridge))

    return {
        "ok": True,
        "target": {"x": round(tx, 6), "y": round(ty, 6)},
        # center 不额外舍入：确认加入后重算时不因舍入越过边界容差
        "center": {"x": fx / w, "y": fy / h},
        "centerMm": {"x": round(fx, 6), "y": round(fy, 6)},
        "foldedWidth": round(w, 6),
        "foldedHeight": round(h, 6),
        "holes": holes,
        "holeCount": len(holes),
    }


def plan_layout(payload):
    """目标孔位排版：原纸目标孔心集合 -> 折叠面打孔方案。

    每个目标先折回最终折叠面、再展开为完整连带孔组；全部连带孔按坐标
    容差去重后与目标集合比对。只有二者完全一致、方案打孔点数不超过
    MAX_HOLES 且通过边界/折痕/保护区/纸桥净距校验时才可行；否则指出缺少的
    连带孔或违规目标及原因。存在违规目标或纸桥冲突时不给出预览（部分方案
    会误导）。纸桥净距按替换后的方案孔位全集判定。
    """
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")

    width, height, radius, folds, min_bridge = _validate_sheet(payload)
    zones = _validate_zones(payload)

    targets = payload.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("缺少目标孔心 targets（原纸 mm 坐标的非空数组）")
    if len(targets) > MAX_TARGETS:
        raise ValueError(f"目标孔心数量须为 1 ~ {MAX_TARGETS} 个")
    norm_targets = []
    for i, t in enumerate(targets):
        if not isinstance(t, dict):
            raise ValueError(f"目标孔心 #{i + 1} 格式错误")
        norm_targets.append(
            {"x": _num(t, "x", 0.0, MAX_DIM, f"目标孔心 #{i + 1} 的 x"),
             "y": _num(t, "y", 0.0, MAX_DIM, f"目标孔心 #{i + 1} 的 y")})

    w, h, edges, nv, nh = folded_state(width, height, folds)
    _check_face_capacity(w, h, radius)

    # 保护区必须完整位于原纸内
    oob = zone_out_of_bounds(zones, width, height)
    if oob is not None:
        raise ValueError(zone_bounds_msg(oob, zones[oob], width, height))

    n_images = 2 ** len(folds)
    violations = []
    folded = []      # 通过边界/折痕校验的目标：(下标, 原纸 x, 原纸 y, 面 x, 面 y)

    for i, t in enumerate(norm_targets):
        tx, ty = t["x"], t["y"]
        reason = None
        if tx > width + EDGE_TOL or ty > height + EDGE_TOL:
            reason = (f"目标孔心 ({tx:.2f}, {ty:.2f}) 超出原纸 "
                      f"{width:g} × {height:g}mm 的范围，请修正坐标。")
        if reason is None:
            # 目标孔心在原纸上的校验（边界 + 全部折痕）
            reason = check_target_on_paper(tx, ty, width, height, radius, nv, nh)
        fx = fy = None
        if reason is None:
            fx, fy = fold_point_to_face(tx, ty, folds, width, height)
            # 折回折叠面后的校验（与上面的校验几何等价，作浮点兜底）
            err = check_point(fx, fy, w, h, radius, edges)
            if err:
                reason = f"折回折叠面后{err}"
        if reason is not None:
            violations.append({"index": i,
                               "target": {"x": round(tx, 6), "y": round(ty, 6)},
                               "reason": reason})
        else:
            folded.append((i, tx, ty, fx, fy))

    # 折回后的打孔点按坐标容差去重（折叠排版中大量目标共享同一打孔点），
    # 之后每个唯一打孔点只展开一次，避免逐目标展开的平方级开销
    uniq_face, face_of = dedupe_with_index([(fx, fy) for _, _, _, fx, fy
                                            in folded])
    face_dedup = [{"x": round(x, 6), "y": round(y, 6)} for x, y in uniq_face]
    too_many = len(uniq_face) > MAX_HOLES

    groups = []      # 每个唯一打孔点的完整连带孔组（未去重）
    if not too_many:
        # 打孔点超限时不再展开：方案必然不可用，且避免产生超限的巨型结果
        for fx, fy in uniq_face:
            groups.append(unfold_points([{"x": fx, "y": fy}],
                                        folds, width, height))
        if zones:
            # 与折叠面打孔同一判定：任一连带孔圆触碰保护区即违规；
            # 共享同一打孔点的目标一并判违规
            hit_by_group = {}
            for gi, raw in enumerate(groups):
                hit = find_zone_hit(raw, zones, radius, n_images)
                if hit:
                    hit_by_group[gi] = hit
            for k, (i, tx, ty, _, _) in enumerate(folded):
                hit = hit_by_group.get(face_of[k])
                if hit:
                    _, hole, zi = hit
                    violations.append({
                        "index": i,
                        "target": {"x": round(tx, 6), "y": round(ty, 6)},
                        "reason": zone_hit_msg(
                            f"目标 #{i + 1}（原纸 ({tx:.2f}, {ty:.2f})mm）",
                            hole["x"], hole["y"], zi, zones[zi], radius)})
            violations.sort(key=lambda v: v["index"])

    feasible = False
    holes, missing, points = [], [], []
    folded_svg = unfolded_svg = None
    error = None
    bridge_error = None

    if violations:
        error = (f"{len(violations)} 个目标孔心违规"
                 f"（首个：{violations[0]['reason']}）")
        if too_many:
            error += (f"；且方案需要 {len(uniq_face)} 个打孔点，"
                      f"超过上限 {MAX_HOLES} 个")
        error += "，方案不可用。"
    elif too_many:
        error = (f"方案需要 {len(uniq_face)} 个打孔点，"
                 f"超过上限 {MAX_HOLES} 个，方案不可用。")
    else:
        # 连带孔并集，按坐标容差去重（先合并，再做后续判定）
        uniq_holes = dedupe_points(pt for g in groups for pt in g)

        # 最小纸桥宽度：按替换后的方案判定（替换后的孔位全集）。
        # 检查任意两个不同孔的圆边净距；冲突时不给出可确认方案或可下载
        # SVG，需修改参数后重新计算。
        conflict = find_bridge_conflict(
            [(hp["x"], hp["y"]) for hp in uniq_holes], radius, min_bridge)
        if conflict:
            _ia, _ib, ha, hb, clearance = conflict
            error = bridge_conflict_msg(
                "排版方案中，", ha, hb, clearance, min_bridge)
            bridge_error = bridge_conflict_info(ha, hb, clearance)
        else:
            holes = uniq_holes

            # 缺少的连带孔：必然会被打出、却未出现在目标集合中的孔位
            in_targets = point_set_membership(
                [(t["x"], t["y"]) for t in norm_targets])
            missing = [hp for hp in holes if not in_targets(hp["x"], hp["y"])]
            feasible = not missing

            # 归一化坐标不额外舍入（JSON 双精度往返无损）：确认替换后重算时
            # 不会因舍入越过边界容差（check_point 另有 ROUND_TRIP_TOL 宽限兜底）
            points = [{"x": p["x"] / w, "y": p["y"] / h} for p in face_dedup]
            folded_svg = build_folded_svg(w, h, edges, face_dedup, radius, -1)[0]
            unfolded_svg = build_unfolded_svg(
                width, height, holes, crease_grid(width, height, nv, nh),
                radius, nv, nh, zones)

            if missing:
                error = (f"按当前折法会额外带出 {len(missing)} 个连带孔"
                         f"（如 ({missing[0]['x']:g}, {missing[0]['y']:g})mm），"
                         "未包含在目标列表中，方案不可用。")

    return {
        "ok": True,
        "feasible": feasible,
        "points": points,
        "pointCount": len(face_dedup),
        "maxHoles": MAX_HOLES,
        "holes": holes,
        "holeCount": len(holes),
        "targetCount": len(norm_targets),
        "missing": missing,
        "violations": violations,
        "foldedWidth": round(w, 6),
        "foldedHeight": round(h, 6),
        "foldedSvg": folded_svg,
        "unfoldedSvg": unfolded_svg,
        "error": error,
        "bridgeError": bridge_error,
    }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template(
        "index.html",
        max_folds=MAX_FOLDS, max_holes=MAX_HOLES, max_zones=MAX_ZONES,
        max_targets=MAX_TARGETS,
        min_dim=MIN_DIM, max_dim=MAX_DIM,
        min_radius=MIN_RADIUS, max_radius=MAX_RADIUS)


@app.route("/api/unfold", methods=["POST"])
def api_unfold():
    payload = request.get_json(force=True, silent=True)
    if payload is None:
        return _bad("请求不是合法 JSON")
    try:
        return jsonify(compute(payload))
    except ValueError as exc:
        return _bad(str(exc))


@app.route("/api/reverse", methods=["POST"])
def api_reverse():
    """反查打孔：入参 {width, height, radius, folds, zones, target:{x,y}(原纸 mm)}。"""
    payload = request.get_json(force=True, silent=True)
    if payload is None:
        return _bad("请求不是合法 JSON")
    try:
        return jsonify(reverse_lookup(payload))
    except ValueError as exc:
        return _bad(str(exc))


@app.route("/api/plan", methods=["POST"])
def api_plan():
    """目标孔位排版：入参 {width, height, radius, folds, zones,
    targets: [{x, y} 原纸 mm]}。"""
    payload = request.get_json(force=True, silent=True)
    if payload is None:
        return _bad("请求不是合法 JSON")
    try:
        return jsonify(plan_layout(payload))
    except ValueError as exc:
        return _bad(str(exc))


@app.route("/api/download", methods=["POST"])
def api_download():
    """只在计算合法时返回可下载的展开 SVG 文件。"""
    payload = request.get_json(force=True, silent=True)
    if payload is None:
        return _bad("请求不是合法 JSON")
    try:
        result = compute(payload)
    except ValueError as exc:
        return _bad(str(exc))
    if result["error"] or not result["unfoldedSvg"]:
        return _bad(result["error"] or "存在不合法孔位，无法生成展开图")
    return Response(
        result["unfoldedSvg"],
        mimetype="image/svg+xml",
        headers={"Content-Disposition":
                 'attachment; filename="unfolded.svg"'})


@app.route("/api/download-a4", methods=["POST"])
def api_download_a4():
    """只在展开结果合法（含孔位/保护区/纸桥全部校验）时返回 A4 分页
    定位模板 ZIP：纵向 A4 1:1 mm，四边留白 10mm、相邻页重叠 15mm。"""
    payload = request.get_json(force=True, silent=True)
    if payload is None:
        return _bad("请求不是合法 JSON")
    try:
        result = compute(payload)
        if result["error"] or not result["unfoldedSvg"]:
            raise ValueError(result["error"] or "存在不合法孔位，无法生成 A4 分页模板")
        width, height, radius, folds, _min_bridge = _validate_sheet(payload)
        zones = _validate_zones(payload)
        nv = sum(1 for f in folds if f in ("vL", "vR"))
        nh = len(folds) - nv
        data = build_a4_zip(result, width, height, radius, nv, nh, zones)
    except ValueError as exc:
        return _bad(str(exc))
    return Response(
        data,
        mimetype="application/zip",
        headers={"Content-Disposition":
                 'attachment; filename="a4-pages.zip"'})


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
