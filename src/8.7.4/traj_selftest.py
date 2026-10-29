"""轨迹算法自检工具。

两条检查线：

    一、自洽性（硬约束，服务端可交叉校验）
        - 五类字段结构完整、类型正确
        - collectTime / startTime / endTime 单调
        - sum(paceList.distance) == finish.distance
        - stepList 末段 endStep == sum(paceList.stepCount) == finish.totalStep
        - duration == pois 首末时间差
        - strideList 距离累计 == finish.distance
        - targetPoints.clockTime 落在轨迹区间内

    二、分布（真人特征，防止被统计指纹识别）
        - 采样间隔标准差 > 50ms
        - accuracy / stability 不是常数
        - 方位角二阶差分标准差 > 2°
        - 配速切片 CV >= 0.05、步幅切片 CV >= 0.03、分段步数标准差 >= 1

用法：
    python traj_selftest.py            # 跑全部场景
    python traj_selftest.py --seed 7   # 只跑指定种子
"""
import argparse
import os
import sys
import time
from typing import Any, Dict, List, Tuple

cur_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, cur_dir)

from running_protocol import generate_trajectory_data, trajectory_metrics

START_LAT = 36.6512
START_LNG = 117.1201

# 典型高校操场／校园环线周边的打卡点（相对起跑点 150~420 米）
WAYPOINTS = [
    {"code": "WP001", "lat": START_LAT + 0.0012, "lng": START_LNG + 0.0021, "type": 1},
    {"code": "WP002", "lat": START_LAT + 0.0028, "lng": START_LNG + 0.0004, "type": 1},
    {"code": "WP003", "lat": START_LAT + 0.0016, "lng": START_LNG - 0.0019, "type": 1},
]

SCENARIOS: List[Dict[str, Any]] = [
    {"name": "操场 2km / 6'00\"", "distance": 2000, "pace": 360, "shape": "track"},
    {"name": "操场 1.6km / 5'00\"", "distance": 1600, "pace": 300, "shape": "track"},
    {"name": "公园环线 3km / 7'00\"", "distance": 3000, "pace": 420, "shape": "loop"},
    {"name": "街区 2.4km / 6'30\"", "distance": 2400, "pace": 390, "shape": "grid"},
    {"name": "打卡点 2km / 6'00\"", "distance": 2000, "pace": 360, "shape": "waypoints", "waypoints": WAYPOINTS},
    {"name": "短距 800m / 5'30\"", "distance": 800, "pace": 330, "shape": "track"},
    {"name": "长距 5km / 6'00\"", "distance": 5000, "pace": 360, "shape": "loop"},
]


def _fail(msg: str) -> None:
    print(f"  [失败] {msg}")
    raise AssertionError(msg)


def _check(cond: bool, msg: str) -> None:
    if not cond:
        _fail(msg)


def check_schema(data: Dict[str, Any]) -> None:
    for i, p in enumerate(data["pois"]):
        _check(set(p) == {"lat", "lng", "stability", "accuracy", "collectTime"}, f"pois[{i}] 字段不符: {sorted(p)}")
        _check(isinstance(p["stability"], int) and isinstance(p["collectTime"], int), f"pois[{i}] 类型不符")
    for i, s in enumerate(data["stepList"]):
        _check(
            set(s) == {"index", "startTime", "endTime", "startStep", "endStep", "step", "time", "stability"},
            f"stepList[{i}] 字段不符: {sorted(s)}",
        )
    for i, s in enumerate(data["paceList"]):
        _check(
            set(s) == {
                "index", "startTime", "endTime", "startDistance", "endDistance", "distance",
                "time", "startStepCount", "endStepCount", "stepCount", "stability",
            },
            f"paceList[{i}] 字段不符: {sorted(s)}",
        )
    for i, s in enumerate(data["strideList"]):
        _check(set(s) == {"index", "time", "distance", "stride", "step"}, f"strideList[{i}] 字段不符: {sorted(s)}")
    for i, s in enumerate(data["targetPoints"]):
        _check(
            set(s) == {"code", "lat", "lng", "type", "passStatus", "clockTime"},
            f"targetPoints[{i}] 字段不符: {sorted(s)}",
        )


def check_consistency(data: Dict[str, Any], target_distance: float) -> Dict[str, Any]:
    pois = data["pois"]
    steps = data["stepList"]
    paces = data["paceList"]
    strides = data["strideList"]
    summary = data["summary"]

    _check(len(pois) >= 30, f"采样点过少: {len(pois)}")
    _check(len(steps) >= 3, f"步数切片过少: {len(steps)}")
    _check(len(paces) >= 3, f"配速切片过少: {len(paces)}")

    times = [p["collectTime"] for p in pois]
    _check(all(b > a for a, b in zip(times, times[1:])), "collectTime 非严格递增")

    for i in range(1, len(steps)):
        _check(steps[i]["startTime"] == steps[i - 1]["endTime"], f"stepList[{i}] 时间轴不连续")
        _check(steps[i]["endTime"] > steps[i]["startTime"], f"stepList[{i}] 时长非正")
        _check(steps[i]["startStep"] == steps[i - 1]["endStep"], f"stepList[{i}] 步数轴不连续")
    for i in range(1, len(paces)):
        _check(paces[i]["startTime"] == paces[i - 1]["endTime"], f"paceList[{i}] 时间轴不连续")
        _check(paces[i]["startDistance"] == paces[i - 1]["endDistance"], f"paceList[{i}] 距离轴不连续")
        _check(paces[i]["startStepCount"] == paces[i - 1]["endStepCount"], f"paceList[{i}] 步数轴不连续")

    _check(steps[0]["startTime"] == times[0], "stepList 起点与首个采样点不一致")
    _check(abs(steps[-1]["endTime"] - times[-1]) <= 1500, "stepList 终点与末个采样点偏差过大")
    _check(abs(paces[-1]["endTime"] - times[-1]) <= 1500, "paceList 终点与末个采样点偏差过大")

    sum_pace_dist = sum(p["distance"] for p in paces)
    _check(sum_pace_dist == summary["distance"], f"配速距离合计 {sum_pace_dist} != 里程 {summary['distance']}")
    _check(paces[-1]["endDistance"] == summary["distance"], "配速末尾距离与里程不一致")

    sum_pace_steps = sum(p["stepCount"] for p in paces)
    _check(sum_pace_steps == summary["totalStep"], f"配速步数合计 {sum_pace_steps} != 总步数 {summary['totalStep']}")
    _check(steps[-1]["endStep"] == summary["totalStep"], "stepList 末段步数与总步数不一致")

    sum_stride_dist = sum(s["distance"] for s in strides)
    _check(abs(sum_stride_dist - summary["distance"]) <= 3, f"步幅距离合计 {sum_stride_dist} 与里程偏差过大")

    span = (times[-1] - times[0]) / 1000.0
    _check(abs(span - summary["duration"]) <= 2.0, f"轨迹时长 {span}s 与 duration {summary['duration']}s 不一致")

    _check(summary["distance"] >= target_distance * 0.99, f"里程 {summary['distance']} 明显低于目标 {target_distance}")
    _check(summary["distance"] <= target_distance * 1.03, f"里程 {summary['distance']} 明显超过目标 {target_distance}")

    _check(times[-1] <= int(time.time() * 1000), "轨迹末端时间落在未来（会被时序校验抓到）")

    for tp in data["targetPoints"]:
        _check(times[0] <= tp["clockTime"] <= times[-1], f"打卡点 {tp['code']} 时刻越界")

    duration_by_pace = sum(p["time"] for p in paces) / 1000.0
    _check(abs(duration_by_pace - summary["duration"]) <= 1.0, "配速切片时长合计与 duration 不一致")

    return {
        "paceDistance": sum_pace_dist,
        "paceSteps": sum_pace_steps,
        "strideDistance": sum_stride_dist,
        "poiSpan": round(span, 1),
    }


def check_realism(data: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """按统计指纹检查是否符合真机分布；返回 (指标, 触发的告警列表)。"""
    m = trajectory_metrics(data)
    flags: List[str] = []

    if m.get("intervalStdMs", 0) < 50:
        flags.append("采样间隔过于等距")
    if m.get("stabilityDistinct", 0) < 2:
        flags.append("stability 恒定")
    if m.get("accuracyDistinct", 0) < 3:
        flags.append("accuracy 方差过小")
    if m.get("headingDeltaStd", 0) < 2:
        flags.append("方位角过于平滑")
    if m.get("paceCv", 0) < 0.05:
        flags.append("配速曲线过于平直")
    if m.get("strideCv", 0) < 0.03:
        flags.append("步幅方差过小")
    if m.get("stepSegStd", 0) < 1.0:
        flags.append("分段步数过于整齐")
    if m.get("timeMonotonic") is False:
        flags.append("采样时间非单调")

    # GPS 噪声导致的里程膨胀，真机 1Hz 采样通常 3%~12%
    dist = m.get("distance") or 1
    infl = m.get("distanceFromPois", 0) / dist - 1.0
    if infl < 0.01:
        flags.append("原始采样点累计里程几乎没有噪声膨胀")
    if infl > 0.15:
        flags.append(f"采样点累计里程膨胀过大({infl:.1%})")

    for tp in data.get("targetPoints") or []:
        if not tp.get("passStatus"):
            flags.append(f"打卡点 {tp.get('code')} 未通过")
    return m, flags


def run_scenario(sc: Dict[str, Any], seed: int) -> bool:
    data = generate_trajectory_data(
        START_LAT,
        START_LNG,
        sc["distance"],
        sc["pace"],
        seed=seed,
        shape=sc.get("shape", "auto"),
        waypoints=sc.get("waypoints"),
    )
    ok = True
    try:
        check_schema(data)
        extra = check_consistency(data, sc["distance"])
    except AssertionError:
        ok = False
        extra = {}

    m, flags = check_realism(data)
    if flags:
        ok = False

    s = data["summary"]
    print(
        f"  {sc['name']:<18} 里程 {s['distance']:>5}m  用时 {s['duration']:>4}s  "
        f"步数 {s['totalStep']:>4}  配速 {s['avgPace']}s/km  步频 {s['avgFrequency']}spm  "
        f"点数 {s['poiCount']:>3}  片 {len(data['paceList'])}/{len(data['strideList'])}"
    )
    print(
        f"  {'':<18} CV: 配速 {m.get('paceCv', 0):.3f} 步幅 {m.get('strideCv', 0):.3f} | "
        f"间隔σ {m.get('intervalStdMs', 0):.0f}ms | 方位σ {m.get('headingDeltaStd', 0):.1f}° | "
        f"分段步数σ {m.get('stepSegStd', 0):.2f} | 打卡 {sum(1 for t in data['targetPoints'] if t['passStatus'])}/{len(data['targetPoints'])}"
    )
    if flags:
        print(f"  {'':<18} [告警] {'；'.join(flags)}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="轨迹算法自检")
    parser.add_argument("--seed", type=int, default=20261022, help="随机种子")
    args = parser.parse_args()

    print(f"[自检] 轨迹算法 种子={args.seed} 场景数={len(SCENARIOS)}")
    t0 = time.time()
    results = []
    for sc in SCENARIOS:
        ok = run_scenario(sc, args.seed)
        results.append((sc["name"], ok))

    # 同一参数在连续种子下必须稳定，不能出现个别种子塌陷
    print("[自检] 种子稳定性 20 连跑 (操场 2km / 6'00\")")
    bad = []
    for seed in range(args.seed, args.seed + 20):
        data = generate_trajectory_data(START_LAT, START_LNG, 2000, 360, seed=seed, shape="track")
        m, flags = check_realism(data)
        if flags:
            bad.append((seed, flags))
    if bad:
        for seed, flags in bad:
            print(f"  种子 {seed}: {flags}")
    else:
        print("  20/20 通过")

    elapsed = time.time() - t0
    failed = [n for n, ok in results if not ok]
    if failed or bad:
        print(f"[自检] 未通过 {len(failed) + len(bad)} 项，耗时 {elapsed:.2f}s")
        return 1
    print(f"[自检] 全部通过，耗时 {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
