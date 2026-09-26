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
- A4 分页定位模板：把合法展开结果按实际毫米 1:1 排到纵向 A4 页
  （210×297mm，四边留白 10mm，可打印区 190×277mm，相邻页重叠 15mm），
  按原纸尺寸计算覆盖全纸的分页窗口（窗口从原纸左下角起排，列自左而右、
  行自下而上，与原纸坐标方向一致），逐页裁剪纸边、折痕、保护区与孔圆，
  跨页图形在相邻页各保留对应片段，全程不缩放。每页标注行列号、原纸窗口
  坐标与重叠区对位十字，全部页面连同打印拼接说明打包为 ZIP 下载；
  分页入口复用孔位/保护区/纸桥校验，违规时拒绝下载。
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
# A4 分页定位模板（纵向 A4，1:1 实际毫米，不缩放）
# ---------------------------------------------------------------------------
A4_W = 210.0                        # mm，纵向 A4 宽
A4_H = 297.0                        # mm，纵向 A4 高
A4_MARGIN = 10.0                    # mm，四边留白
A4_OVERLAP = 15.0                   # mm，相邻页重叠
A4_PRINT_W = A4_W - 2 * A4_MARGIN               # 190mm，可打印区宽
A4_PRINT_H = A4_H - 2 * A4_MARGIN               # 277mm，可打印区高
A4_STEP_X = A4_PRINT_W - A4_OVERLAP             # 175mm，列步进
A4_STEP_Y = A4_PRINT_H - A4_OVERLAP             # 262mm，行步进


def a4_page_layout(width, height):
    """按原纸尺寸计算覆盖全纸的 A4 分页窗口。

    窗口从原纸左下角 (0,0) 起排：列自左而右、行自下而上（与原纸坐标方向
    一致）。每页窗口即该页可打印区（190×277mm）在原纸上的覆盖范围，
    相邻页窗口恰好重叠 15mm；末列/末行窗口可越出原纸（该页只画纸面部分）。
    """
    cols = max(1, math.ceil((width - A4_PRINT_W) / A4_STEP_X - 1e-9) + 1)
    rows = max(1, math.ceil((height - A4_PRINT_H) / A4_STEP_Y - 1e-9) + 1)
    windows = []
    for row in range(rows):
        for col in range(cols):
            x0 = col * A4_STEP_X
            y0 = row * A4_STEP_Y
            windows.append({"col": col, "row": row, "x0": x0, "y0": y0,
                            "x1": x0 + A4_PRINT_W, "y1": y0 + A4_PRINT_H})
    return {"cols": cols, "rows": rows, "pages": cols * rows,
            "windows": windows}


def _a4_cross(cx, cy):
    """重叠区对位十字（蓝色）：5mm 十字 + 小圆，相邻页同一原纸坐标各画一次。"""
    a = 2.5
    return (f'<g stroke="#2563eb" stroke-width="0.3" fill="none">'
            f'<line x1="{cx - a:.3f}" y1="{cy:.3f}" x2="{cx + a:.3f}" '
            f'y2="{cy:.3f}"/>'
            f'<line x1="{cx:.3f}" y1="{cy - a:.3f}" x2="{cx:.3f}" '
            f'y2="{cy + a:.3f}"/>'
            f'<circle cx="{cx:.3f}" cy="{cy:.3f}" r="1.2"/></g>\n')


def build_a4_page_svg(width, height, holes, creases, zones, radius,
                      layout, win):
    """单页 A4 分页定位模板 SVG（纵向 A4，1:1 mm，不缩放）。

    原纸点 (px, py)（左下角原点、y 向上）映射到页面 SVG 坐标
    (10 + px - wx0, 287 - (py - wy0))；纸边、折痕、保护区与孔圆统一由
    clipPath 裁到本页可打印窗口，跨页图形在相邻页各保留对应片段。
    只写入与本页窗口相交的图形（裁剪结果不变，文件更小）。
    """
    col, row = win["col"], win["row"]
    wx0, wy0, wx1, wy1 = win["x0"], win["y0"], win["x1"], win["y1"]

    def X(px):
        return A4_MARGIN + px - wx0

    def Y(py):
        return A4_H - A4_MARGIN - (py - wy0)

    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n',
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{A4_W:.0f}mm" height="{A4_H:.0f}mm" '
        f'viewBox="0 0 {A4_W:.3f} {A4_H:.3f}">\n',
        f'<desc>A4 分页定位模板：第 {row + 1} 行 第 {col + 1} 列'
        f'（共 {layout["rows"]} 行 × {layout["cols"]} 列）；'
        f'原纸窗口 x {wx0:g} ~ {wx1:g}mm，y {wy0:g} ~ {wy1:g}mm'
        '（原点为原纸左下角，y 轴向上）</desc>\n',
        f'<rect x="0" y="0" width="{A4_W:.3f}" height="{A4_H:.3f}" '
        'fill="#ffffff"/>\n',
        # 可打印区边界（裁切/核对参考）
        f'<rect x="{A4_MARGIN:.3f}" y="{A4_MARGIN:.3f}" '
        f'width="{A4_PRINT_W:.3f}" height="{A4_PRINT_H:.3f}" '
        'fill="none" stroke="#94a3b8" stroke-width="0.25" '
        'stroke-dasharray="3 2"/>\n',
        # 裁剪窗口：纸边、折痕、保护区、孔圆一律裁到本页可打印区
        f'<clipPath id="pg"><rect x="{A4_MARGIN:.3f}" y="{A4_MARGIN:.3f}" '
        f'width="{A4_PRINT_W:.3f}" height="{A4_PRINT_H:.3f}"/></clipPath>\n',
        '<g clip-path="url(#pg)">\n',
        # 纸边（原纸矩形，越出窗口的部分被裁掉）
        f'<rect x="{X(0.0):.3f}" y="{Y(height):.3f}" width="{width:.3f}" '
        f'height="{height:.3f}" fill="#ffffff" stroke="#1f2937" '
        'stroke-width="0.35"/>\n',
    ]
    eps = 1e-9
    for c in creases:
        if c["orient"] == "v":
            if not (wx0 - eps <= c["pos"] <= wx1 + eps):
                continue
            parts.append(
                f'<line x1="{X(c["pos"]):.3f}" y1="{Y(0.0):.3f}" '
                f'x2="{X(c["pos"]):.3f}" y2="{Y(height):.3f}" '
                'stroke="#9ca3af" stroke-width="0.2" stroke-dasharray="2 1.5"/>\n')
        else:
            if not (wy0 - eps <= c["pos"] <= wy1 + eps):
                continue
            parts.append(
                f'<line x1="{X(0.0):.3f}" y1="{Y(c["pos"]):.3f}" '
                f'x2="{X(width):.3f}" y2="{Y(c["pos"]):.3f}" '
                'stroke="#9ca3af" stroke-width="0.2" stroke-dasharray="2 1.5"/>\n')
    for zi, z in enumerate(zones):
        if (z["x"] + z["w"] < wx0 - eps or z["x"] > wx1 + eps
                or z["y"] + z["h"] < wy0 - eps or z["y"] > wy1 + eps):
            continue
        parts.append(
            f'<rect x="{X(z["x"]):.3f}" y="{Y(z["y"] + z["h"]):.3f}" '
            f'width="{z["w"]:.3f}" height="{z["h"]:.3f}" '
            'fill="#16a34a" fill-opacity="0.10" stroke="#15803d" '
            'stroke-width="0.4" stroke-dasharray="2.5 1.8"/>\n')
        if z["w"] >= 8.0 and z["h"] >= 4.5:
            parts.append(
                f'<text x="{X(z["x"]) + 1.0:.3f}" '
                f'y="{Y(z["y"] + z["h"]) + 3.4:.3f}" '
                f'font-size="3" fill="#15803d">保{zi + 1}</text>\n')
    r = float(radius)
    for hp in holes:
        if (hp["x"] + r < wx0 - eps or hp["x"] - r > wx1 + eps
                or hp["y"] + r < wy0 - eps or hp["y"] - r > wy1 + eps):
            continue
        parts.append(_circle(X(hp["x"]), Y(hp["y"]), r))
    parts.append('</g>\n')

    # 重叠区边界（琥珀色点线）与对位十字（蓝色）：只画存在邻页的方向。
    # 十字位于重叠区中线的 1/3、2/3 处，相邻页在同一原纸坐标各画一次。
    crosses = []
    if col > 0:                     # 左邻页：本页左侧 15mm 为重叠区
        parts.append(
            f'<line x1="{A4_MARGIN + A4_OVERLAP:.3f}" y1="{A4_MARGIN:.3f}" '
            f'x2="{A4_MARGIN + A4_OVERLAP:.3f}" y2="{A4_H - A4_MARGIN:.3f}" '
            'stroke="#d97706" stroke-width="0.3" stroke-dasharray="1.5 1.5"/>\n')
        crosses.append((A4_MARGIN + A4_OVERLAP / 2,
                        A4_MARGIN + A4_PRINT_H / 3))
        crosses.append((A4_MARGIN + A4_OVERLAP / 2,
                        A4_MARGIN + 2 * A4_PRINT_H / 3))
    if col < layout["cols"] - 1:    # 右邻页：本页右侧 15mm 为重叠区
        parts.append(
            f'<line x1="{A4_MARGIN + A4_STEP_X:.3f}" y1="{A4_MARGIN:.3f}" '
            f'x2="{A4_MARGIN + A4_STEP_X:.3f}" y2="{A4_H - A4_MARGIN:.3f}" '
            'stroke="#d97706" stroke-width="0.3" stroke-dasharray="1.5 1.5"/>\n')
        crosses.append((A4_MARGIN + A4_PRINT_W - A4_OVERLAP / 2,
                        A4_MARGIN + A4_PRINT_H / 3))
        crosses.append((A4_MARGIN + A4_PRINT_W - A4_OVERLAP / 2,
                        A4_MARGIN + 2 * A4_PRINT_H / 3))
    if row > 0:                     # 下邻页：本页下侧 15mm 为重叠区
        parts.append(
            f'<line x1="{A4_MARGIN:.3f}" '
            f'y1="{A4_H - A4_MARGIN - A4_OVERLAP:.3f}" '
            f'x2="{A4_W - A4_MARGIN:.3f}" '
            f'y2="{A4_H - A4_MARGIN - A4_OVERLAP:.3f}" '
            'stroke="#d97706" stroke-width="0.3" stroke-dasharray="1.5 1.5"/>\n')
        crosses.append((A4_MARGIN + A4_PRINT_W / 3,
                        A4_H - A4_MARGIN - A4_OVERLAP / 2))
        crosses.append((A4_MARGIN + 2 * A4_PRINT_W / 3,
                        A4_H - A4_MARGIN - A4_OVERLAP / 2))
    if row < layout["rows"] - 1:    # 上邻页：本页上侧 15mm 为重叠区
        parts.append(
            f'<line x1="{A4_MARGIN:.3f}" y1="{A4_MARGIN + A4_OVERLAP:.3f}" '
            f'x2="{A4_W - A4_MARGIN:.3f}" y2="{A4_MARGIN + A4_OVERLAP:.3f}" '
            'stroke="#d97706" stroke-width="0.3" stroke-dasharray="1.5 1.5"/>\n')
        crosses.append((A4_MARGIN + A4_PRINT_W / 3,
                        A4_MARGIN + A4_OVERLAP / 2))
        crosses.append((A4_MARGIN + 2 * A4_PRINT_W / 3,
                        A4_MARGIN + A4_OVERLAP / 2))
    for cx, cy in crosses:
        parts.append(_a4_cross(cx, cy))

    # 页眉：行列号 + 原纸窗口坐标；页脚：打印与拼接说明
    seq = row * layout["cols"] + col + 1
    parts.append(
        f'<text x="{A4_MARGIN:.3f}" y="5.6" font-size="3.4" fill="#0f172a">'
        f'A4 分页定位模板 · 第 {row + 1} 行 第 {col + 1} 列'
        f'（共 {layout["rows"]} 行 × {layout["cols"]} 列，'
        f'第 {seq}/{layout["pages"]} 页；行自下而上、列自左而右）</text>\n')
    parts.append(
        f'<text x="{A4_MARGIN:.3f}" y="9.2" font-size="2.8" fill="#334155">'
        f'原纸窗口：x {wx0:g} ~ {wx1:g} mm，y {wy0:g} ~ {wy1:g} mm'
        '（坐标原点为原纸左下角，y 轴向上）</text>\n')
    parts.append(
        f'<text x="{A4_MARGIN:.3f}" y="293.6" font-size="2.8" fill="#334155">'
        f'原纸 {width:g} × {height:g} mm · 孔 {len(holes)} 个 · '
        '按 100% 比例打印（勿缩放）；琥珀色点线侧为 15mm 重叠区，'
        '蓝色十字与邻页对位拼接</text>\n')
    parts.append('</svg>\n')
    return "".join(parts)


def a4_readme_text(width, height, radius, hole_count, layout):
    """ZIP 内的打印与拼接说明（纯文本）。"""
    return (
        "A4 分页定位模板 · 打印与拼接说明\n"
        "================================\n"
        "\n"
        f"原纸尺寸：{width:g} × {height:g} mm；打孔半径 {radius:g} mm；"
        f"孔数 {hole_count} 个。\n"
        f"分页：共 {layout['pages']} 页（{layout['rows']} 行 × "
        f"{layout['cols']} 列），文件名 a4_page_r{{行}}_c{{列}}.svg；"
        "行号自下而上、列号自左而右，与原纸左下角坐标系一致。\n"
        "分页参数：纵向 A4（210 × 297 mm），四边留白 10 mm，"
        "可打印区 190 × 277 mm，相邻页重叠 15 mm，"
        "全程 1:1 实际毫米，未缩放。\n"
        "\n"
        "打印\n"
        "----\n"
        "1. 务必按 100% 比例（实际大小）打印：关闭「适应页面」"
        "「缩放至可打印区域」等选项。\n"
        "2. 打印后可用直尺核对：可打印区虚线框应为 190 × 277 mm，"
        "否则说明打印时被缩放，拼接会错位。\n"
        "\n"
        "拼接\n"
        "----\n"
        "1. 每页页眉标有「第 R 行 第 C 列」与原纸窗口坐标"
        "（坐标原点为原纸左下角，y 轴向上）。\n"
        "2. 同行相邻页左右重叠 15 mm：把右页左缘的蓝色对位十字"
        "与左页右缘的对位十字重合（同一原纸坐标）。\n"
        "3. 同列相邻页上下重叠 15 mm：行号大的页在上方，同样对齐对位十字。\n"
        "4. 建议从第 1 行（最下一行）开始，逐行向上、每行自左而右拼接，"
        "即还原整张原纸。\n"
        "5. 图例：琥珀色点线 = 重叠区边界（距可打印区边缘 15 mm）；"
        "灰虚线 = 折痕；绿色虚线框 = 保护区；红圆 = 孔位；"
        "蓝色十字 = 重叠区对位标记。\n"
    )


def build_a4_zip(width, height, holes, creases, zones, radius):
    """把全部 A4 分页 SVG 与打印拼接说明打包为 ZIP（内存字节）。"""
    layout = a4_page_layout(width, height)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for win in layout["windows"]:
            name = f"a4_page_r{win['row'] + 1}_c{win['col'] + 1}.svg"
            zf.writestr(name, build_a4_page_svg(
                width, height, holes, creases, zones, radius, layout, win))
        zf.writestr("README.txt", a4_readme_text(
            width, height, radius, len(holes), layout))
    return buf.getvalue(), layout


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

    # A4 分页窗口只依赖纸张尺寸，违规时同样返回（页面据此显示页数）
    a4 = a4_page_layout(width, height)

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
        "a4Pages": {"cols": a4["cols"], "rows": a4["rows"],
                    "pages": a4["pages"],
                    "printWidth": A4_PRINT_W, "printHeight": A4_PRINT_H,
                    "margin": A4_MARGIN, "overlap": A4_OVERLAP},
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
    """A4 分页定位模板：合法展开结果 1:1 排到纵向 A4 页，打包 ZIP 下载。

    与 /api/download 同一入口校验（孔位/保护区/纸桥净距），违规时拒绝下载。
    """
    payload = request.get_json(force=True, silent=True)
    if payload is None:
        return _bad("请求不是合法 JSON")
    try:
        result = compute(payload)
    except ValueError as exc:
        return _bad(str(exc))
    if result["error"] or not result["unfoldedSvg"]:
        return _bad(result["error"] or "存在不合法孔位，无法生成分页模板")
    # compute 已通过全部校验，此处取回纸张尺寸与保护区用于逐页裁剪
    width, height, radius, _folds, _min_bridge = _validate_sheet(payload)
    zones = _validate_zones(payload)
    data, _layout = build_a4_zip(width, height, result["holes"],
                                 result["creases"], zones, radius)
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
