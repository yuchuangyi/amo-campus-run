"""检测判定探针。

用途：用**最小代价**跑一次真实跑单，然后把服务端的判定结果读回来，
回答一个问题 —— 这条数据到底被判成什么。

三种用法，风险从零到一：

    1) 只读规则（不起跑，零痕迹）
       python risk_probe.py --config-only

    2) 只回查某条已有跑单
       python risk_probe.py --detail RUN_RECORD_CODE

    3) 跑一次最小里程并回查判定
       python risk_probe.py --lat 36.6512 --lng 117.1201 -d 800 -p 420

判定字段来源：
    run/runRecordDetail  ->  status / validDistance / auditDetail /
                             faceCheckInfoList / cheatType / targetPoints

status 语义（与 history_cli.py 一致）：
    1、2  正常；3 作弊/无效；4 机审未通过
"""
import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple

cur_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, cur_dir)

from logger import log_status, log_fields, log_error, log_detail
from running_protocol import generate_trajectory_data, trajectory_metrics

STATUS_TEXT = {
    0: "未开始",
    1: "正常/达标",
    2: "正常",
    3: "作弊/无效",
    4: "机审未通过",
}

VERDICT_CLEAN = "未被判定"
VERDICT_DEDUCT = "被扣里程"
VERDICT_CHEAT = "命中作弊判定"
VERDICT_AUDIT = "机审未通过"
VERDICT_POINT = "打卡点未过"
VERDICT_UNKNOWN = "无法判定"


# ------------------------------------------------------------------ 判定
def verdict(detail: Dict[str, Any], expect_distance: int) -> Tuple[str, List[str]]:
    """把 run_record_detail 的返回压成一句结论 + 依据列表。"""
    reasons: List[str] = []
    status = detail.get("status")
    valid = detail.get("validDistance")
    if valid is None:
        valid = detail.get("distance", 0) or 0

    audit = detail.get("auditDetail") or detail.get("auditResult") or []
    if audit:
        reasons.append(f"机审命中: {json.dumps(audit, ensure_ascii=False)[:200]}")

    face = detail.get("faceCheckInfoList") or detail.get("faceCheckRecordList") or []
    if isinstance(face, list) and face:
        def _passed(item: Dict[str, Any]) -> bool:
            v = item.get("passStatus", item.get("status", True))
            if isinstance(v, bool):
                return v
            return str(v).lower() in ("1", "true", "pass")

        failed_face = [f for f in face if isinstance(f, dict) and not _passed(f)]
        reasons.append(f"人脸校验 {len(face) - len(failed_face)}/{len(face)} 通过")
        if failed_face:
            reasons.append("存在未通过的人脸校验记录")

    cheat = detail.get("cheatType")
    if cheat not in (None, "", 0, "0", []):
        reasons.append(f"cheatType={cheat}")

    tp = detail.get("targetPoints") or []
    if tp:
        miss = [t for t in tp if isinstance(t, dict) and not t.get("passStatus", False)]
        if miss:
            reasons.append(f"必经打卡点未过 {len(miss)}/{len(tp)}")

    if status == 3:
        return VERDICT_CHEAT, reasons
    if status == 4:
        return VERDICT_AUDIT, reasons
    if status in (1, 2):
        if expect_distance and valid and valid < expect_distance * 0.95:
            reasons.append(f"有效里程 {valid}m < 目标 {expect_distance}m 的 95%")
            return VERDICT_DEDUCT, reasons
        if tp:
            miss = [t for t in tp if isinstance(t, dict) and not t.get("passStatus", False)]
            if miss:
                return VERDICT_POINT, reasons
        return VERDICT_CLEAN, reasons
    reasons.append(f"status={status} 不在已知语义内")
    return VERDICT_UNKNOWN, reasons


def print_detail(detail: Dict[str, Any], expect_distance: int = 0) -> Tuple[str, List[str]]:
    log_status("跑单回查", ok=True)
    log_fields(
        有效里程=f"{detail.get('validDistance', detail.get('distance', 0))}m",
        耗时=f"{detail.get('duration', 0)}s",
        总步数=detail.get("totalStep", 0),
        状态=f"{detail.get('status')} ({STATUS_TEXT.get(detail.get('status'), '未知')})",
        机审=len(detail.get("auditDetail") or []),
        人脸记录=len(detail.get("faceCheckInfoList") or []),
    )
    level, reasons = verdict(detail, expect_distance)
    log_status("判定", ok=(level == VERDICT_CLEAN), status_text=level)
    for r in reasons:
        log_fields(依据=r)
    return level, reasons


# ------------------------------------------------------------------ 只读
def probe_config(client: Any, args) -> int:
    plans = client.get_run_plan_list()
    if plans.get("code") != 0:
        log_status("读取跑步方案", ok=False)
        log_error(plans.get("message", "查询失败"))
        log_detail(plans)
        return 2
    items = (plans.get("data") or {}).get("list") or []
    log_fields(方案数=len(items))
    for p in items[:5]:
        log_fields(
            方案=p.get("runPlanCode"),
            名称=p.get("runPlanName") or p.get("planName"),
            目标里程=p.get("targetDistance") or p.get("distance"),
        )

    plan_code = args.plan_code or (items[0].get("runPlanCode") if items else "")
    cfg = client.check_sun_run_config(
        run_plan_code=plan_code,
        fence_code=args.fence_code,
        target_distance=str(args.distance),
        sport_type="1",
    )
    if cfg.get("code") != 0:
        log_status("读取跑步配置", ok=False)
        log_error(cfg.get("message", "查询失败"))
        log_detail(cfg)
        return 2
    data = cfg.get("data") or {}
    log_status("读取跑步配置", ok=True)
    log_detail(data)

    # 从配置里挑出与判定直接相关的字段
    keys = [k for k in data if any(s in k.lower() for s in ("face", "check", "distance", "pace", "rule", "fence"))]
    if keys:
        log_status("判定相关字段", ok=True)
        for k in keys:
            log_fields(**{k: data[k]})
    return 0


# ------------------------------------------------------------------ 实跑
def run_flow(client: Any, args, start_lat: float, start_lng: float) -> int:
    plans = client.get_run_plan_list()
    items = (plans.get("data") or {}).get("list") or [] if plans.get("code") == 0 else []
    plan_code = args.plan_code or (items[0].get("runPlanCode") if items else "")

    start = client.start_sun_run_v2(
        run_plan_code=plan_code,
        target_distance=str(args.distance),
        lng=f"{start_lng:.6f}",
        lat=f"{start_lat:.6f}",
    )
    if start.get("code") != 0:
        log_status("起跑", ok=False)
        log_error(start.get("message", "起跑失败"))
        log_detail(start)
        return 2
    code = (start.get("data") or {}).get("runRecordCode")
    log_status("起跑", ok=True)
    log_fields(跑单编号=code)

    traj = generate_trajectory_data(
        start_lat, start_lng, args.distance, args.pace,
        seed=args.seed, shape=args.shape,
    )
    m = trajectory_metrics(traj)
    log_status("轨迹生成", ok=True)
    log_fields(里程=f"{traj['summary']['distance']}m", 用时=f"{traj['summary']['duration']}s",
               配速CV=m.get("paceCv"), 步幅CV=m.get("strideCv"))

    for label, payload in (
        ("轨迹点", lambda: client.upload_run_record(code, traj["pois"])),
        ("步数切片", lambda: client.upload_steps_record(code, traj["stepList"], 20)),
        ("配速切片", lambda: client.upload_pace_record(code, traj["paceList"], 50)),
        ("步幅切片", lambda: client.upload_stride_record(code, traj["strideList"], 200)),
    ):
        res = payload()
        if res.get("code") != 0:
            log_status(f"{label}上传", ok=False)
            log_error(res.get("message", "失败"))
            log_detail(res)
            if args.clean:
                client.abandon_unfinished_run(code)
                log_status("清理挂单", ok=True)
            return 3
        log_status(f"{label}上传", ok=True)

    fin = client.finish_sun_run_v2(
        run_record_code=code,
        distance=traj["summary"]["distance"],
        target_points=traj["targetPoints"],
        step_interval=20,
        duration=traj["summary"]["duration"],
    )
    log_status("结算", ok=(fin.get("code") == 0))
    if fin.get("code") != 0:
        log_error(fin.get("message", "结算失败"))
        log_detail(fin)
        if args.clean:
            client.abandon_unfinished_run(code)
            log_status("清理挂单", ok=True)
        return 4
    log_detail(fin.get("data"))

    if args.settle_wait > 0:
        log_status("等待机审", ok=True, status_text=f"{args.settle_wait}s")
        time.sleep(args.settle_wait)

    detail_res = client.run_record_detail(code)
    if detail_res.get("code") != 0:
        log_status("跑单回查", ok=False)
        log_error(detail_res.get("message", "查询失败"))
        return 5

    detail = detail_res.get("data") or {}
    level, _ = print_detail(detail, traj["summary"]["distance"])
    log_fields(跑单编号=code)
    return 0 if level == VERDICT_CLEAN else 6


def main() -> int:
    parser = argparse.ArgumentParser(description="跑单判定探针")
    parser.add_argument("--config-only", action="store_true", help="只读跑步规则，不起跑")
    parser.add_argument("--detail", default="", help="只回查指定跑单")
    parser.add_argument("-d", "--distance", type=int, default=800, help="目标里程 (米)，默认 800")
    parser.add_argument("-p", "--pace", type=int, default=420, help="平均配速 (秒/公里)，默认 420")
    parser.add_argument("--plan-code", default="", help="跑步方案编号")
    parser.add_argument("--fence-code", default="", help="围栏编号")
    parser.add_argument("--lat", type=float, default=0.0)
    parser.add_argument("--lng", type=float, default=0.0)
    parser.add_argument("--shape", default="auto", choices=["auto", "track", "loop", "grid", "waypoints"])
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--settle-wait", type=int, default=5, help="结算后等待机审的秒数")
    parser.add_argument("--clean", action="store_true", help="失败时调用 abnormalFinishSunRun 清理挂单")
    parser.add_argument("--token", default=None)
    parser.add_argument("--satoken", default=None)
    parser.add_argument("--transport", default="direct", choices=["direct", "adb", "proxy"])
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--adb-path", default=None)
    parser.add_argument("--adb-device", default=None)
    args = parser.parse_args()

    from client import ShanDongClient

    client = ShanDongClient(
        token=args.token, satoken=args.satoken, transport_mode=args.transport,
        proxy=args.proxy, adb_path=args.adb_path, adb_device=args.adb_device,
    )
    if not client.transport.token:
        log_status("载入会话", ok=False)
        log_error("没有可用会话，先跑 login_cli.py")
        return 1

    if args.detail:
        res = client.run_record_detail(args.detail)
        if res.get("code") != 0:
            log_error(res.get("message", "查询失败"))
            log_detail(res)
            return 2
        print_detail(res.get("data") or {}, args.distance)
        return 0

    if args.config_only:
        return probe_config(client, args)

    if not (args.lat and args.lng):
        log_status("起跑坐标", ok=False)
        log_error("实跑需要 --lat / --lng")
        return 1
    return run_flow(client, args, args.lat, args.lng)


if __name__ == "__main__":
    sys.exit(main())
