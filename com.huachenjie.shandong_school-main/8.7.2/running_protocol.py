"""跑步轨迹模拟算法（8.7.2 数据包规范）。

把「一次跑步」建模成一份可直接上报的数据包：

    pois / stepList / paceList / strideList / targetPoints

并保证五类字段之间能互相印证（服务端就是拿这些字段交叉校验的）：

    距离   sum(paceList[].distance) == finish.distance
    步数   stepList[-1].endStep    == sum(paceList[].stepCount) == finish.totalStep
    时间   duration                == (pois[-1].collectTime - pois[0].collectTime) / 1000
    时间轴 stepList / paceList / strideList 的 startTime / endTime 与 pois 同源递增
    打卡   targetPoints[].clockTime 落在轨迹真实经过该点的时刻附近

生成管线：

    1. 路线建模（局部 ENU 平面，支持跑道 / 环线 / 街区 / 指定打卡点）
    2. 配速曲线（起跑加速 + 体能衰减 + 地形慢波 + 慢漂移 + 快抖动 + 偶发事件）
    3. 逐秒积分，得到「弧长 s ↔ 时间 t」的单调映射
    4. GPS 采样（系统调度抖动、偶发丢点、信号中断段、位置噪声 AR(1)、多径尖峰）
    5. 按 20s / 50m / 200m 切片，反算 stepList / paceList / strideList

时间单位约定：所有 *Time / collectTime 为毫秒时间戳；finish 的 duration 为秒。
"""
from __future__ import annotations

import math
import random
import statistics
import time
from bisect import bisect_right
from typing import Any, Dict, List, Optional, Sequence, Tuple

_TWO_PI = math.pi * 2.0
_POINT = Tuple[float, float]


# ==========================================================================
# 一、几何工具（局部 ENU：x 向东，y 向北，单位米）
# ==========================================================================
def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _meters_per_deg(lat: float) -> Tuple[float, float]:
    """该纬度下 1° 纬度 / 1° 经度对应的米数（WGS84 近似，误差 < 0.01%）。"""
    phi = math.radians(_clamp(lat, -89.5, 89.5))
    m_lat = 111132.954 - 559.822 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi)
    m_lng = 111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi) + 0.118 * math.cos(5 * phi)
    return m_lat, max(1.0, abs(m_lng))


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def _enu_to_latlng(lat0: float, lng0: float, x: float, y: float) -> _POINT:
    m_lat, m_lng = _meters_per_deg(lat0)
    return lat0 + y / m_lat, lng0 + x / m_lng


def _latlng_to_enu(lat0: float, lng0: float, lat: float, lng: float) -> _POINT:
    m_lat, m_lng = _meters_per_deg(lat0)
    return (lng - lng0) * m_lng, (lat - lat0) * m_lat


def _bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


class _Polyline:
    """ENU 折线，支持按弧长取点与切向。"""

    __slots__ = ("pts", "cum", "closed", "length")

    def __init__(self, pts: Sequence[_POINT], closed: bool = False):
        pts = [(float(p[0]), float(p[1])) for p in pts]
        if closed and len(pts) > 1 and (pts[0][0] != pts[-1][0] or pts[0][1] != pts[-1][1]):
            pts = pts + [pts[0]]
        self.pts = pts
        self.closed = closed
        cum = [0.0]
        for a, b in zip(pts, pts[1:]):
            cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        self.cum = cum
        self.length = cum[-1]

    def at(self, s: float) -> Tuple[float, float, float, float]:
        """返回 (x, y, 单位切向 tx, ty)，s 会被夹到 [0, length]。"""
        if self.length <= 0.0 or len(self.pts) < 2:
            p = self.pts[0] if self.pts else (0.0, 0.0)
            return p[0], p[1], 1.0, 0.0
        s = _clamp(s, 0.0, self.length)
        i = min(max(bisect_right(self.cum, s) - 1, 0), len(self.pts) - 2)
        seg = self.cum[i + 1] - self.cum[i]
        f = 0.0 if seg <= 0.0 else (s - self.cum[i]) / seg
        x0, y0 = self.pts[i]
        x1, y1 = self.pts[i + 1]
        dx, dy = x1 - x0, y1 - y0
        n = math.hypot(dx, dy) or 1.0
        return x0 + dx * f, y0 + dy * f, dx / n, dy / n


class _GeoPath:
    """经纬度折线，按大地线弧长取点。"""

    __slots__ = ("pts", "cum", "length")

    def __init__(self, pts: Sequence[_POINT]):
        self.pts = [(float(p[0]), float(p[1])) for p in pts]
        cum = [0.0]
        for a, b in zip(self.pts, self.pts[1:]):
            cum.append(cum[-1] + _haversine_m(a[0], a[1], b[0], b[1]))
        self.cum = cum
        self.length = cum[-1]

    def at(self, s: float) -> _POINT:
        if self.length <= 0.0 or len(self.pts) < 2:
            return self.pts[0] if self.pts else (0.0, 0.0)
        s = _clamp(s, 0.0, self.length)
        i = min(max(bisect_right(self.cum, s) - 1, 0), len(self.pts) - 2)
        seg = self.cum[i + 1] - self.cum[i]
        f = 0.0 if seg <= 0.0 else (s - self.cum[i]) / seg
        lat0, lng0 = self.pts[i]
        lat1, lng1 = self.pts[i + 1]
        return lat0 + (lat1 - lat0) * f, lng0 + (lng1 - lng0) * f


# ==========================================================================
# 二、路线建模
# ==========================================================================
def _line(p0: _POINT, p1: _POINT, step: float = 2.0) -> List[_POINT]:
    d = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    n = max(1, int(d / step))
    return [(p0[0] + (p1[0] - p0[0]) * i / n, p0[1] + (p1[1] - p0[1]) * i / n) for i in range(n + 1)]


def _arc(cx: float, cy: float, r: float, a0: float, a1: float, step: float = 2.0) -> List[_POINT]:
    n = max(2, int(abs(a1 - a0) * r / step))
    return [
        (cx + r * math.cos(a0 + (a1 - a0) * i / n), cy + r * math.sin(a0 + (a1 - a0) * i / n))
        for i in range(n + 1)
    ]


def _catmull_closed(points: Sequence[_POINT], per_seg: int = 20) -> List[_POINT]:
    """闭合 Catmull-Rom 样条，曲线精确经过每个控制点。"""
    n = len(points)
    if n < 3:
        return list(points)
    out: List[_POINT] = []
    for i in range(n):
        p0 = points[(i - 1) % n]
        p1 = points[i]
        p2 = points[(i + 1) % n]
        p3 = points[(i + 2) % n]
        for k in range(per_seg):
            t = k / per_seg
            t2, t3 = t * t, t * t * t
            x = 0.5 * (
                2 * p1[0]
                + (-p0[0] + p2[0]) * t
                + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3
            )
            y = 0.5 * (
                2 * p1[1]
                + (-p0[1] + p2[1]) * t
                + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3
            )
            out.append((x, y))
    return out


def _lap_track(rng: random.Random) -> List[_POINT]:
    """标准 400m 塑胶跑道：两直道 + 两半圆，周长 400m，随机旋转。"""
    r = 36.5
    straight = (400.0 - 2 * math.pi * r) / 2.0  # 85.33m
    half = straight / 2.0
    pts: List[_POINT] = []
    pts += _line((-half, -r), (half, -r))
    pts += _arc(half, 0.0, r, -math.pi / 2, math.pi / 2)
    pts += _line((half, r), (-half, r))
    pts += _arc(-half, 0.0, r, math.pi / 2, 3 * math.pi / 2)
    theta = rng.uniform(0.0, _TWO_PI)
    c, s = math.cos(theta), math.sin(theta)
    return [(x * c - y * s, x * s + y * c) for x, y in pts]


def _lap_loop(rng: random.Random, target_perimeter: float) -> List[_POINT]:
    """公园环线：带谐波扰动的闭合曲线，周长按目标值缩放。"""
    p1, p2, p3 = rng.uniform(0, _TWO_PI), rng.uniform(0, _TWO_PI), rng.uniform(0, _TWO_PI)
    a1, a2, a3 = 0.062, 0.034, 0.018
    n = 480

    def build(scale: float) -> List[_POINT]:
        out = []
        for k in range(n):
            th = _TWO_PI * k / n
            rr = 1.0 + a1 * math.sin(2 * th + p1) + a2 * math.sin(3 * th + p2) + a3 * math.sin(5 * th + p3)
            out.append((scale * rr * math.cos(th), scale * rr * math.sin(th)))
        return out

    base = build(1.0)
    perim = _Polyline(base).length
    return build(target_perimeter / max(1.0, perim))


def _lap_grid(rng: random.Random, target_perimeter: float) -> List[_POINT]:
    """街区跑：圆角矩形，两直道 + 四个 90° 转角。"""
    aspect = rng.uniform(1.0, 1.7)
    r = rng.uniform(5.0, 12.0)
    # 2w + 2h - 8r + 2πr = P
    h = (target_perimeter - _TWO_PI * r + 8 * r) / (2 * (aspect + 1))
    w = aspect * h
    w, h = max(w, 60.0), max(h, 60.0)
    x0, x1 = -w / 2 + r, w / 2 - r
    y0, y1 = -h / 2 + r, h / 2 - r
    pts: List[_POINT] = []
    pts += _line((x0, -h / 2), (x1, -h / 2))                      # 下直道
    pts += _arc(x1, y0, r, -math.pi / 2, 0.0)                     # 右下角
    pts += _line((w / 2, y0), (w / 2, y1))                        # 右直道
    pts += _arc(x1, y1, r, 0.0, math.pi / 2)                      # 右上角
    pts += _line((x1, h / 2), (x0, h / 2))                        # 上直道
    pts += _arc(x0, y1, r, math.pi / 2, math.pi)                  # 左上角
    pts += _line((-w / 2, y1), (-w / 2, y0))                      # 左直道
    pts += _arc(x0, y0, r, math.pi, 3 * math.pi / 2)              # 左下角
    theta = rng.uniform(0.0, _TWO_PI)
    c, s = math.cos(theta), math.sin(theta)
    return [(x * c - y * s, x * s + y * c) for x, y in pts]


def _inflate_lap(pts: Sequence[_POINT], target_len: float, rng: random.Random) -> List[_POINT]:
    """在保持控制点不动的前提下，把绕行路径向外扩到目标周长。

    相邻控制点之间插入一个沿「远离质心」方向偏移的中点，二分法求偏移量。
    """
    base = list(pts)
    cx = sum(p[0] for p in base) / len(base)
    cy = sum(p[1] for p in base) / len(base)

    def build(k: float) -> List[_POINT]:
        out: List[_POINT] = []
        for i, p in enumerate(base):
            q = base[(i + 1) % len(base)]
            out.append(p)
            mx, my = (p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0
            vx, vy = mx - cx, my - cy
            n = math.hypot(vx, vy)
            if n < 1e-6:
                dx, dy = q[1] - p[1], -(q[0] - p[0])
                n = math.hypot(dx, dy) or 1.0
                vx, vy = dx / n, dy / n
            else:
                vx, vy = vx / n, vy / n
            out.append((mx + vx * k, my + vy * k))
        return _catmull_closed(out)

    lo, hi = 0.0, 240.0
    for _ in range(9):
        mid = (lo + hi) / 2.0
        if _Polyline(build(mid), closed=True).length < target_len:
            lo = mid
        else:
            hi = mid
    return build(lo)


def _lap_waypoints(rng: random.Random, waypoints_enu: Sequence[_POINT], target_len: float) -> List[_POINT]:
    """按必经打卡点构造闭合回路：起点 + 各打卡点，必要时外扩绕行。"""
    ctrl: List[_POINT] = [(0.0, 0.0)]
    for p in waypoints_enu:
        if math.hypot(p[0] - ctrl[-1][0], p[1] - ctrl[-1][1]) > 3.0:
            ctrl.append((p[0], p[1]))

    if len(ctrl) == 1:
        # 一个点都没有 / 都在起点附近：退回环线
        return _lap_loop(rng, max(300.0, target_len))
    if len(ctrl) == 2:
        (xa, ya), (xb, yb) = ctrl
        mx, my = (xa + xb) / 2, (ya + yb) / 2
        dx, dy = xb - xa, yb - ya
        d = math.hypot(dx, dy) or 200.0
        nx, ny = -dy / d, dx / d
        h = max(60.0, target_len * 0.16)
        ctrl = [(xa, ya), (mx + nx * h, my + ny * h), (xb, yb), (mx - nx * h, my - ny * h)]

    lap = _catmull_closed(ctrl)
    if _Polyline(lap, closed=True).length < target_len:
        lap = _inflate_lap(ctrl, target_len, rng)
    return lap


def _build_route(
    shape: str,
    distance_m: float,
    rng: random.Random,
    waypoints_enu: Optional[Sequence[_POINT]] = None,
) -> List[_POINT]:
    """构造一条覆盖 distance_m 的 ENU 路线，起点严格位于 (0, 0)。

    shape:
        track      400m 标准跑道，绕圈
        loop       公园／校园环线
        grid       街区圆角矩形
        waypoints  经过给定打卡点的闭合回路
    """
    if shape == "auto":
        shape = "waypoints" if waypoints_enu else "track"

    if shape == "track":
        lap = _lap_track(rng)
    elif shape == "loop":
        n_laps = max(1, int(round(distance_m / 700.0)))
        lap = _lap_loop(rng, _clamp(distance_m / n_laps, 260.0, 1500.0))
    elif shape == "grid":
        n_laps = max(1, int(round(distance_m / 900.0)))
        lap = _lap_grid(rng, _clamp(distance_m / n_laps, 300.0, 1800.0))
    elif shape == "waypoints":
        lap = _lap_waypoints(rng, list(waypoints_enu or []), _clamp(distance_m, 260.0, 2600.0) * 0.9)
    else:
        raise ValueError(f"未知路线类型: {shape}")

    lap_poly = _Polyline(lap, closed=True)
    perim = lap_poly.length
    if perim <= 1.0:
        raise ValueError("路线周长异常，无法生成轨迹")

    # 随机起跑相位：从环线上任意一点起跑，而不是固定从几何起点。
    # 打卡点路线的几何起点就是起跑坐标，相位必须锁定在 0，否则整圈会整体偏移。
    s0 = 0.0 if shape == "waypoints" else rng.uniform(0.0, perim)
    x0, y0, _, _ = lap_poly.at(s0)

    # 横向漂移：真实跑者不会每一圈压在同一条线上
    drift_amp = 0.8 if shape == "track" else 1.4
    ph1, ph2 = rng.uniform(0, _TWO_PI), rng.uniform(0, _TWO_PI)

    step = 2.0
    n = max(8, int(math.ceil(distance_m / step)))
    route: List[_POINT] = []
    for i in range(n + 1):
        s = distance_m * i / n
        x, y, tx, ty = lap_poly.at((s0 + s) % perim)
        # 法向 = 切向顺时针旋转 90°
        nx, ny = -ty, tx
        off = drift_amp * math.sin(_TWO_PI * s / (perim * 2.3) + ph1)
        off += 0.45 * drift_amp * math.sin(_TWO_PI * s / 97.0 + ph2)
        route.append((x + nx * off, y + ny * off))

    # 平移，使起点落在 ENU 原点
    rx, ry = x0, y0
    return [(p[0] - rx, p[1] - ry) for p in route]


# ==========================================================================
# 三、配速曲线
# ==========================================================================
def _speed_profile(
    duration_s: int,
    rng: random.Random,
    level: float = 1.0,
) -> List[float]:
    """生成逐秒相对速度序列，均值归一化到 1。

    构成：起跑加速、体能衰减、地形慢波、慢漂移 AR(1)、快抖动 AR(1)、偶发事件。
    """
    t_end = max(1, duration_s - 1)
    # 先定全程速度波动幅度，再拆成「地形慢波」与「体能衰减」两部分，
    # 避免随机抽样把两者都抽到下限，导致整段配速过于平直。
    variation = rng.uniform(0.130, 0.190) * level
    hill_share = rng.uniform(0.45, 0.65)
    period = rng.uniform(150.0, 420.0)
    amp = variation * hill_share
    phase = rng.uniform(0.0, _TWO_PI)
    fatigue = variation * (1.0 - hill_share) * 0.9
    drift_sigma = 0.009 * level
    fast_sigma = 0.011 * level

    raw: List[float] = []
    drift = 0.0
    fast = 0.0
    for t in range(duration_s):
        warm = 1.0 - 0.18 * math.exp(-t / 22.0)
        fade = 1.0 - fatigue * (t / t_end)
        hill = amp * math.sin(_TWO_PI * t / period + phase)
        drift = 0.985 * drift + rng.gauss(0.0, drift_sigma)
        fast = 0.68 * fast + rng.gauss(0.0, fast_sigma)
        raw.append(max(0.55, warm * fade * (1.0 + hill + drift + fast)))

    # 偶发事件：路口／避让减速，或短距离冲刺
    i = 0
    while i < duration_s:
        roll = rng.random()
        if roll < 0.0035:
            dur = rng.randint(2, 6)
            depth = rng.uniform(0.12, 0.30)
            for k in range(dur):
                if i + k < duration_s:
                    raw[i + k] *= 1.0 - depth * math.sin(math.pi * (k + 0.5) / dur)
            i += dur
        elif roll < 0.0060:
            dur = rng.randint(3, 8)
            boost = rng.uniform(0.06, 0.14)
            for k in range(dur):
                if i + k < duration_s:
                    raw[i + k] *= 1.0 + boost * math.sin(math.pi * (k + 0.5) / dur)
            i += dur
        else:
            i += 1

    for _ in range(2):  # 归一化到均值 1，并做一次软限幅
        mean = sum(raw) / len(raw)
        raw = [_clamp(v / mean, 0.72, 1.32) for v in raw]

    # 配速波动下限：14 秒滑窗（约 50 米一个配速切片）的变异系数
    raw = _enforce_rolling_cv(raw, window=14, floor=0.062, cap=2.4)
    mean = sum(raw) / len(raw) or 1.0
    return [v / mean for v in raw]


# ==========================================================================
# 四、GPS 采样
# ==========================================================================
def _fix_times(duration_s: int, rng: random.Random, sample_hz: float = 1.0) -> List[float]:
    """生成采集时刻序列（秒，相对起跑）。

    真实设备：1Hz 上下浮动，偶发丢点、偶发重复点，另有一段信号中断。
    """
    base = 1.0 / max(0.2, sample_hz)
    times: List[float] = []
    t = 0.0
    guard = 0
    while t <= duration_s + 1e-6 and guard < 200000:
        guard += 1
        times.append(t)
        step = base + rng.gauss(0.0, 0.075)
        if rng.random() < 0.020:            # 丢点：系统调度晚到
            step += rng.uniform(0.5, 1.8)
        elif rng.random() < 0.045:          # 重复点：短时间内连续回调
            step = rng.uniform(0.18, 0.55)
        t += max(0.05, step)

    # 信号中断：连续 3~8 秒完全没有采样
    if duration_s > 120 and rng.random() < 0.55:
        gaps = 1 if rng.random() < 0.7 else 2
        for _ in range(gaps):
            start = rng.uniform(duration_s * 0.08, duration_s * 0.92)
            width = rng.uniform(3.0, 8.0)
            times = [x for x in times if not (start < x < start + width)] or times

    times = sorted(set(round(x, 3) for x in times))
    if times[0] > 0.0:
        times.insert(0, 0.0)
    if times[-1] < duration_s:
        times.append(float(duration_s))
    return times


def _position_noise(n: int, rng: random.Random, level: float = 1.0) -> List[_POINT]:
    """位置噪声：设备系统性偏移 + AR(1) 相关抖动 + 偶发多径尖峰。"""
    bias_x, bias_y = rng.gauss(0.0, 1.2 * level), rng.gauss(0.0, 1.2 * level)
    nx = ny = 0.0
    out: List[_POINT] = []
    for _ in range(n):
        nx = 0.72 * nx + rng.gauss(0.0, 0.80 * level)
        ny = 0.72 * ny + rng.gauss(0.0, 0.80 * level)
        dx, dy = nx + bias_x, ny + bias_y
        if rng.random() < 0.008:  # 多径／高楼反射
            ang = rng.uniform(0.0, _TWO_PI)
            mag = rng.uniform(3.0, 8.0) * level
            dx += math.cos(ang) * mag
            dy += math.sin(ang) * mag
        out.append((dx, dy))
    return out


def _accuracy_series(times: Sequence[float], rng: random.Random, level: float = 1.0) -> List[float]:
    base = rng.uniform(5.0, 8.5) * level
    period = rng.uniform(70.0, 160.0)
    phase = rng.uniform(0.0, _TWO_PI)
    out: List[float] = []
    for t in times:
        a = base + 1.5 * math.sin(_TWO_PI * t / period + phase) + rng.gauss(0.0, 0.7)
        if rng.random() < 0.035:  # 遮挡／重捕获
            a += rng.uniform(5.0, 18.0)
        out.append(round(_clamp(a, 3.5, 40.0), 1))
    return out


def _stability_series(accuracies: Sequence[float], rng: random.Random) -> List[int]:
    """信号稳定度：与定位精度反相关，带随机翻转，保证不是常数。"""
    out: List[int] = []
    for a in accuracies:
        s = 4 if a <= 7.0 else 3 if a <= 10.0 else 2 if a <= 16.0 else 1
        if rng.random() < 0.12:
            s -= 1
        elif rng.random() < 0.10:
            s += 1
        out.append(int(_clamp(s, 1, 4)))
    if len(set(out)) == 1:  # 极端情况兜底，避免整段恒定
        for i in range(0, len(out), 9):
            out[i] = 4 if out[i] < 4 else 3
    return out


def _enforce_rolling_cv(
    series: Sequence[float],
    window: int,
    floor: float,
    cap: float = 2.2,
) -> List[float]:
    """保证低频波动不低于下限。

    随机抽样的慢波偶尔会同相抵消，使整段过于平直；这里在保持曲线形状的前提下，
    以均值为中心等比放大偏离量，二分求出刚好达到下限的放大系数。
    """
    vals = list(series)
    n = len(vals)
    window = max(4, window)
    if n < window * 2:
        return vals
    step = max(1, window // 3)
    base = sum(vals) / n

    def cv_of(k: float) -> float:
        amp = [base + (v - base) * k for v in vals]
        rolled = [sum(amp[i:i + window]) / window for i in range(0, n - window + 1, step)]
        m = sum(rolled) / len(rolled)
        if m == 0.0:
            return 0.0
        sd = (sum((x - m) ** 2 for x in rolled) / len(rolled)) ** 0.5
        return sd / abs(m)

    if cv_of(1.0) >= floor:
        return vals

    lo, hi = 1.0, max(1.0, cap)
    if cv_of(hi) < floor:
        k = hi
    else:
        for _ in range(12):
            mid = (lo + hi) / 2.0
            if cv_of(mid) < floor:
                lo = mid
            else:
                hi = mid
        k = hi
    return [base + (v - base) * k for v in vals]


def _slice_bounds(total: float, interval: float, min_tail: float) -> List[float]:
    """等距切片边界；尾段过短时并入上一段，避免出现 2 米的畸形切片。"""
    interval = max(1.0, float(interval))
    bounds: List[float] = []
    d = interval
    while d < total - 1.0:
        bounds.append(d)
        d += interval
    if not bounds:
        return [total]
    if total - bounds[-1] < min_tail:
        bounds.pop()
    bounds.append(total)
    return bounds


def _interp(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect_right(xs, x) - 1
    i = min(max(i, 0), len(xs) - 2)
    span = xs[i + 1] - xs[i]
    f = 0.0 if span <= 0 else (x - xs[i]) / span
    return ys[i] + (ys[i + 1] - ys[i]) * f


# ==========================================================================
# 五、主入口
# ==========================================================================
def generate_trajectory_data(
    start_lat: float = 0.0,
    start_lng: float = 0.0,
    target_distance_m: float = 2000.0,
    avg_pace_sec_per_km: int = 360,
    start_timestamp_ms: Optional[int] = None,
    *,
    waypoints: Optional[Sequence[Dict[str, Any]]] = None,
    shape: str = "auto",
    seed: Optional[int] = None,
    sample_hz: float = 1.0,
    noise_level: float = 1.0,
    pass_radius_m: float = 25.0,
    step_interval_s: int = 20,
    pace_interval_m: int = 50,
    stride_interval_m: int = 200,
    index_base: int = 1,
    backdate: bool = True,
) -> Dict[str, Any]:
    """生成一份完整、自洽的跑步数据包。

    参数
    ----
    start_lat / start_lng     起跑坐标（WGS84）
    target_distance_m         目标里程（米），实际里程会有 ±0.3% 浮动
    avg_pace_sec_per_km       平均配速（秒/公里），例：360 = 6'00"/km
    start_timestamp_ms        起跑时刻（毫秒）；None 时自动回填到今天，保证数据早于当前时刻
    waypoints                 必经打卡点 [{"code": 1, "lat": .., "lng": .., "type": 1}, ...]
    shape                     track / loop / grid / waypoints / auto
    seed                      随机种子，固定后可复现
    pass_radius_m             打卡点判定半径
    step_interval_s           步数切片间隔（秒）
    pace_interval_m           配速切片间隔（米）
    stride_interval_m         步幅切片间隔（米）

    返回
    ----
    {"pois": [...], "stepList": [...], "paceList": [...], "strideList": [...],
     "targetPoints": [...], "summary": {...}, "meta": {...}}
    """
    rng = random.Random(seed)

    distance_req = float(max(100.0, target_distance_m))
    pace = int(_clamp(float(avg_pace_sec_per_km), 150.0, 900.0))
    duration_s = int(round(distance_req * pace / 1000.0))
    duration_s = max(30, duration_s)
    mean_speed = distance_req / duration_s  # m/s

    # ---- 1. 打卡点（ENU 相对起跑点）----
    wp_list: List[Dict[str, Any]] = []
    wp_enu: List[_POINT] = []
    for idx, wp in enumerate(waypoints or []):
        lat = float(wp.get("lat", 0.0))
        lng = float(wp.get("lng", 0.0))
        if abs(lat) < 1e-9 and abs(lng) < 1e-9:
            continue
        wp_enu.append(_latlng_to_enu(start_lat, start_lng, lat, lng))
        wp_list.append(
            {
                "code": wp.get("code", idx + 1),
                "lat": lat,
                "lng": lng,
                "type": int(wp.get("type", 1)),
            }
        )

    # ---- 2. 路线 ----
    eff_shape = "waypoints" if (shape == "auto" and wp_enu) else shape
    if eff_shape == "waypoints":
        # 打卡点必须落在原始坐标上，不能做等比缩放，只能多跑一点点
        route_enu = _build_route(eff_shape, distance_req * 1.002, rng, wp_enu)
    else:
        route_enu = _build_route(eff_shape, distance_req, rng, wp_enu)
        # 以 ENU 长度做一次等比校正，保证上报里程 >= 目标里程
        enu_len = _Polyline(route_enu).length
        if enu_len > 1.0:
            k = (distance_req + 1.0) / enu_len
            route_enu = [(x * k, y * k) for x, y in route_enu]
    route_ll = [_enu_to_latlng(start_lat, start_lng, x, y) for x, y in route_enu]
    path = _GeoPath(route_ll)
    total_distance = path.length

    # ---- 3. 配速曲线与逐秒积分 ----
    profile = _speed_profile(duration_s, rng, noise_level)
    sec_speed = [mean_speed * v for v in profile]
    raw_cum = [0.0]
    for v in sec_speed:
        raw_cum.append(raw_cum[-1] + v)
    if raw_cum[-1] <= 0:
        raise ValueError("配速曲线异常：累计里程为 0")
    scale = total_distance / raw_cum[-1]
    dist_cum = [s * scale for s in raw_cum]
    time_grid = [float(i) for i in range(duration_s + 1)]

    def distance_at(t: float) -> float:
        return _interp(time_grid, dist_cum, t)

    def time_at(d: float) -> float:
        return _interp(dist_cum, time_grid, d)

    # ---- 4. 步频与步数累计 ----
    cadence_base = rng.uniform(158.0, 170.0)
    cad_phase = rng.uniform(0.0, _TWO_PI)
    cad_period = rng.uniform(55.0, 95.0)
    cad_amp = rng.uniform(3.0, 4.8)
    cad_slow_period = rng.uniform(140.0, 260.0)
    cad_slow_amp = rng.uniform(4.0, 6.5)
    cad_slow_phase = rng.uniform(0.0, _TWO_PI)
    cad_vslow_period = rng.uniform(300.0, 700.0)
    cad_vslow_amp = rng.uniform(5.5, 9.0)
    cad_vslow_phase = rng.uniform(0.0, _TWO_PI)
    cadence_drift = 0.0
    cad_series: List[float] = []
    for t in range(duration_s):
        v_rel = sec_speed[t] / mean_speed
        cadence_drift = 0.985 * cadence_drift + rng.gauss(0.0, 0.9)
        cadence = cadence_base + 30.0 * (v_rel - 1.0)
        cadence += cad_amp * math.sin(_TWO_PI * t / cad_period + cad_phase)
        cadence += cad_slow_amp * math.sin(_TWO_PI * t / cad_slow_period + cad_slow_phase)
        cadence += cad_vslow_amp * math.sin(_TWO_PI * t / cad_vslow_period + cad_vslow_phase)
        cadence += cadence_drift + rng.gauss(0.0, 1.2)
        cad_series.append(_clamp(cadence, 130.0, 210.0))

    # 步幅波动下限：72 秒滑窗（约 200 米一个步幅切片）的步频变异系数
    cad_series = _enforce_rolling_cv(cad_series, window=72, floor=0.036, cap=3.0)
    steps_cum = [0.0]
    for c in cad_series:
        steps_cum.append(steps_cum[-1] + _clamp(c, 130.0, 210.0) / 60.0)

    def steps_at(t: float) -> float:
        return _interp(time_grid, steps_cum, t)

    def step_index(t: float) -> int:
        return int(math.floor(steps_at(t)))

    if start_timestamp_ms is None:
        now_ms = int(time.time() * 1000)
        start_timestamp_ms = now_ms - int(duration_s * 1000) - (90_000 if backdate else 0)
    start_ts = int(start_timestamp_ms)

    # ---- 5. GPS 采样 ----
    fix_times = _fix_times(duration_s, rng, sample_hz)
    n_fix = len(fix_times)
    noise = _position_noise(n_fix, rng, noise_level)
    accuracies = _accuracy_series(fix_times, rng, noise_level)
    stabilities = _stability_series(accuracies, rng)

    pois: List[Dict[str, Any]] = []
    for i, t in enumerate(fix_times):
        s = distance_at(t)
        lat, lng = path.at(s)
        m_lat, m_lng = _meters_per_deg(lat)
        dx, dy = noise[i]
        lat_n = lat + dy / m_lat
        lng_n = lng + dx / m_lng
        pois.append(
            {
                "lat": round(lat_n, 6),
                "lng": round(lng_n, 6),
                "stability": int(stabilities[i]),
                "accuracy": float(accuracies[i]),
                "collectTime": start_ts + int(round(t * 1000.0)),
            }
        )
    # 时间戳严格单调（重复点合并后可能撞同一毫秒）
    for i in range(1, len(pois)):
        if pois[i]["collectTime"] <= pois[i - 1]["collectTime"]:
            pois[i]["collectTime"] = pois[i - 1]["collectTime"] + (1 if i % 3 else 2)

    # ---- 6. stepList：每 20 秒一段 ----
    step_boundaries = list(range(0, duration_s, max(5, step_interval_s)))
    if not step_boundaries:
        step_boundaries = [0]
    if step_boundaries[-1] != duration_s:
        if len(step_boundaries) > 1 and duration_s - step_boundaries[-1] < 8:
            step_boundaries.pop()  # 尾段不足 8 秒，并入上一段
        step_boundaries.append(duration_s)

    step_list: List[Dict[str, Any]] = []
    for idx in range(len(step_boundaries) - 1):
        t0, t1 = float(step_boundaries[idx]), float(step_boundaries[idx + 1])
        st0, st1 = step_index(t0), step_index(t1)
        seg_time = int(round((t1 - t0) * 1000.0))
        step_list.append(
            {
                "index": idx + index_base,
                "startTime": start_ts + int(round(t0 * 1000.0)),
                "endTime": start_ts + int(round(t1 * 1000.0)),
                "startStep": st0,
                "endStep": st1,
                "step": max(0, st1 - st0),
                "time": seg_time,
                "stability": int(_clamp(3 + (1 if idx % 5 == 0 else 0), 1, 4)),
            }
        )
    total_step = step_index(float(duration_s))

    # ---- 7. paceList：每 50 米一段 ----
    pace_bounds = _slice_bounds(total_distance, max(10, pace_interval_m), max(20.0, pace_interval_m * 0.4))

    pace_list: List[Dict[str, Any]] = []
    prev_d = 0.0
    prev_t = 0.0
    prev_step = step_index(0.0)
    for idx, bound in enumerate(pace_bounds):
        t = time_at(bound)
        cur_step = step_index(t)
        d_start = int(round(prev_d))
        d_end = int(round(bound))
        t_start = int(round(prev_t * 1000.0))
        t_end = int(round(t * 1000.0))
        pace_list.append(
            {
                "index": idx + index_base,
                "startTime": start_ts + t_start,
                "endTime": start_ts + t_end,
                "startDistance": d_start,
                "endDistance": d_end,
                "distance": max(0, d_end - d_start),
                "time": max(1, t_end - t_start),
                "startStepCount": prev_step,
                "endStepCount": cur_step,
                "stepCount": max(0, cur_step - prev_step),
                "stability": int(_clamp(3 + (1 if idx % 4 else 0), 1, 4)),
            }
        )
        prev_d, prev_t, prev_step = bound, t, cur_step

    # ---- 8. strideList：每 200 米一段 ----
    stride_bounds = _slice_bounds(total_distance, max(50, stride_interval_m), max(50.0, stride_interval_m * 0.4))

    stride_list: List[Dict[str, Any]] = []
    prev_d, prev_t, prev_step = 0.0, 0.0, step_index(0.0)
    for idx, bound in enumerate(stride_bounds):
        t = time_at(bound)
        cur_step = step_index(t)
        seg_dist = max(0.0, bound - prev_d)
        seg_steps = max(1, cur_step - prev_step)
        stride_cm = int(round(seg_dist / seg_steps * 100.0))
        stride_list.append(
            {
                "index": idx + index_base,
                "time": max(1, int(round((t - prev_t) * 1000.0))),
                "distance": int(round(seg_dist)),
                "stride": stride_cm,
                "step": int(cur_step - prev_step),
            }
        )
        prev_d, prev_t, prev_step = bound, t, cur_step

    # ---- 9. targetPoints：按轨迹最近点判定打卡 ----
    target_points: List[Dict[str, Any]] = []
    for wp in wp_list:
        best_i, best_d = 0, float("inf")
        for i, p in enumerate(pois):
            dm = _haversine_m(p["lat"], p["lng"], wp["lat"], wp["lng"])
            if dm < best_d:
                best_i, best_d = i, dm
        target_points.append(
            {
                "code": wp["code"],
                "lat": wp["lat"],
                "lng": wp["lng"],
                "type": wp["type"],
                "passStatus": bool(best_d <= pass_radius_m),
                "clockTime": pois[best_i]["collectTime"],
            }
        )

    # ---- 10. 汇总 ----
    poi_span_s = (pois[-1]["collectTime"] - pois[0]["collectTime"]) / 1000.0
    summary = {
        "distance": int(round(total_distance)),
        "duration": int(duration_s),
        "totalStep": int(total_step),
        "avgPace": int(round(pace)),
        "avgFrequency": int(round(total_step / max(1.0, duration_s) * 60.0)),
        "startTime": start_ts,
        "endTime": start_ts + int(duration_s * 1000),
        "poiCount": len(pois),
        "poiSpanSec": round(poi_span_s, 1),
    }
    meta = {
        "seed": seed,
        "shape": shape,
        "routeLength": round(total_distance, 1),
        "meanSpeed": round(mean_speed, 3),
        "stepInterval": max(5, step_interval_s),
        "paceInterval": max(10, pace_interval_m),
        "strideInterval": max(50, stride_interval_m),
        "sampleHz": sample_hz,
        "waypointCount": len(wp_list),
    }

    return {
        "pois": pois,
        "stepList": step_list,
        "paceList": pace_list,
        "strideList": stride_list,
        "targetPoints": target_points,
        "summary": summary,
        "meta": meta,
    }


# ==========================================================================
# 六、自检指标（供 CLI 与 traj_selftest.py 复用）
# ==========================================================================
def trajectory_metrics(data: Dict[str, Any]) -> Dict[str, Any]:
    """把生成结果压成一组可核对的统计量，用于确认数据自洽、分布合理。"""
    pois = data.get("pois") or []
    steps = data.get("stepList") or []
    paces = data.get("paceList") or []
    strides = data.get("strideList") or []
    summary = data.get("summary") or {}

    def _cv(values: Sequence[float]) -> float:
        vals = [v for v in values if v is not None]
        if len(vals) < 2:
            return 0.0
        m = sum(vals) / len(vals)
        if m == 0:
            return 0.0
        return statistics.pstdev(vals) / m

    out: Dict[str, Any] = {
        "poiCount": len(pois),
        "stepCount": len(steps),
        "paceCount": len(paces),
        "strideCount": len(strides),
    }

    if len(pois) > 2:
        ts = [p["collectTime"] for p in pois]
        deltas = [b - a for a, b in zip(ts, ts[1:])]
        out["intervalMeanMs"] = round(sum(deltas) / len(deltas), 1)
        out["intervalStdMs"] = round(statistics.pstdev(deltas), 1)
        out["stabilityDistinct"] = len({p.get("stability") for p in pois})
        out["accuracyDistinct"] = len({p.get("accuracy") for p in pois})
        out["timeMonotonic"] = all(b > a for a, b in zip(ts, ts[1:]))

        bearings = [
            _bearing_deg(pois[i]["lat"], pois[i]["lng"], pois[i + 1]["lat"], pois[i + 1]["lng"])
            for i in range(len(pois) - 1)
        ]
        d2 = []
        for i in range(len(bearings) - 1):
            diff = (bearings[i + 1] - bearings[i] + 180.0) % 360.0 - 180.0
            d2.append(abs(diff))
        out["headingDeltaStd"] = round(statistics.pstdev(d2), 2) if len(d2) > 1 else 0.0

        gaps = [
            _haversine_m(pois[i]["lat"], pois[i]["lng"], pois[i + 1]["lat"], pois[i + 1]["lng"])
            for i in range(len(pois) - 1)
        ]
        out["gapCv"] = round(_cv(gaps), 3) if gaps else 0.0
        out["distanceFromPois"] = round(
            sum(_haversine_m(pois[i]["lat"], pois[i]["lng"], pois[i + 1]["lat"], pois[i + 1]["lng"])
                for i in range(len(pois) - 1)),
            1,
        )

    if paces:
        out["paceCv"] = round(_cv([p["distance"] / max(1, p["time"]) for p in paces]), 3)
        out["sumPaceDistance"] = sum(p["distance"] for p in paces)
        out["sumPaceSteps"] = sum(p["stepCount"] for p in paces)
        out["paceTimeSumMs"] = sum(p["time"] for p in paces)

    if strides:
        out["strideCv"] = round(_cv([s["stride"] for s in strides]), 3)
        out["strideMeanCm"] = round(sum(s["stride"] for s in strides) / len(strides), 1)

    if steps:
        out["stepSegStd"] = round(statistics.pstdev([s["step"] for s in steps]), 2)
        out["stepSum"] = int(steps[-1]["endStep"])

    if summary:
        out["distance"] = summary.get("distance")
        out["duration"] = summary.get("duration")
        out["totalStep"] = summary.get("totalStep")
        if pois:
            out["poiSpanSec"] = round((pois[-1]["collectTime"] - pois[0]["collectTime"]) / 1000.0, 1)
            out["poisEndAfterNow"] = pois[-1]["collectTime"] > int(time.time() * 1000)
        out["targetPoints"] = [
            {"code": tp.get("code"), "passed": tp.get("passStatus")} for tp in data.get("targetPoints") or []
        ]
    return out


__all__ = ["generate_trajectory_data", "trajectory_metrics"]

