"""长跑业务调度工具：生成轨迹数据 → 分块上传 → 结算。

流程
----
1. 载入会话，选定跑步方案（阳光跑）
2. 起跑：startSunRun_v2 / startFreeRun，拿到 runRecordCode
3. 用 running_protocol 生成完整数据包（pois / stepList / paceList / strideList / targetPoints）
4. 按时间切片分块上传，模拟真机边跑边传的到达节奏
5. 结算：finishSunRun_v2 / finishFreeRun

参数
----
    python run_cli.py -d 2000 -p 360 --lat 36.65 --lng 117.12
    python run_cli.py -t free -d 1600 -p 330 --lat 36.65 --lng 117.12
    python run_cli.py --dry-run -d 2000 -p 360 --lat 36.65 --lng 117.12 --dump out.json
"""
import argparse
import json
import math
import os
import sys
import time
from typing import Any

cur_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, cur_dir)

from logger import log_status, log_fields, log_error, log_detail
from running_protocol import generate_trajectory_data, trajectory_metrics


# ---------------------------------------------------------------- 参数解析
def _parse_fence(text: str):
    """解析 --fence CODE,LAT,LNG[,TYPE]。"""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 3:
        raise argparse.ArgumentTypeError("格式应为 CODE,LAT,LNG[,TYPE]")
    item = {"code": parts[0], "lat": float(parts[1]), "lng": float(parts[2]), "type": 1}
    if len(parts) > 3 and parts[3]:
        try:
            item["type"] = int(parts[3])
        except ValueError:
            pass
    return item


def _dist_m(lat1, lng1, lat2, lng2):
    """两点球面距离（米）。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def _walk_points(node, out, depth=0):
    """从任意结构的响应里提取 {code, lat, lng} 坐标点。"""
    if depth > 8:
        return
    if isinstance(node, dict):
        lat = node.get("lat", node.get("latitude"))
        lng = node.get("lng", node.get("lon", node.get("longitude")))
        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)) and abs(lat) > 1e-6:
            code = node.get("fenceCode") or node.get("code") or node.get("id") or len(out) + 1
            item = {"code": str(code), "lat": float(lat), "lng": float(lng), "type": 1}
            if not any(abs(p["lat"] - item["lat"]) < 1e-7 and abs(p["lng"] - item["lng"]) < 1e-7 for p in out):
                out.append(item)
        for value in node.values():
            _walk_points(value, out, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _walk_points(value, out, depth + 1)


def _fetch_fence_points(client: Any, fence_code: str = "") -> list:
    """尝试从校园围栏 / 跑前检查接口拿到打卡点坐标。"""
    for call in (
        lambda: client.query_school_fences(),
        lambda: client.filter_usable_fence([fence_code] if fence_code else None),
        lambda: client.get_pre_run_check_in(),
    ):
        try:
            res = call()
        except Exception as exc:  # 接口不可用时继续尝试下一个
            log_error(f"围栏查询异常: {exc}")
            continue
        if isinstance(res, dict) and res.get("code") == 0:
            points = []
            _walk_points(res.get("data"), points)
            if points:
                return points
    return []


# ---------------------------------------------------------------- 上传
def _chunk_by_time(pois, chunk_seconds: int):
    """按采集时间切片，模拟边跑边传。"""
    if not pois:
        return []
    span_ms = max(1, chunk_seconds) * 1000
    base = pois[0]["collectTime"]
    chunks, buf, mark = [], [], 0
    for p in pois:
        if p["collectTime"] - base >= mark + span_ms and buf:
            chunks.append(buf)
            buf = []
            mark += span_ms
        buf.append(p)
    if buf:
        chunks.append(buf)
    # 尾块太短就并入上一块，避免出现只有一两个点的畸形批次
    if len(chunks) > 1:
        avg = sum(len(c) for c in chunks[:-1]) / (len(chunks) - 1)
        if len(chunks[-1]) < max(3.0, avg * 0.3):
            chunks[-2].extend(chunks.pop())
    return chunks


def _chunk_list(items, size: int):
    size = max(1, size)
    return [items[i:i + size] for i in range(0, len(items), size)]


def _upload_chunked(label, chunks, send, delay: float) -> bool:
    ok = 0
    total = sum(len(c) for c in chunks)
    for idx, chunk in enumerate(chunks, 1):
        res = send(chunk)
        if not isinstance(res, dict) or res.get("code") != 0:
            log_status(f"{label} 第 {idx}/{len(chunks)} 块", ok=False)
            log_error((res or {}).get("message", "上传失败"))
            log_detail(res)
            return False
        ok += len(chunk)
        log_status(f"{label} 第 {idx}/{len(chunks)} 块", ok=True, status_text=f"{ok}/{total}")
        if idx < len(chunks) and delay > 0:
            time.sleep(delay)
    return True


# ---------------------------------------------------------------- 生成
def _print_summary(traj, metrics, dump_path: str = ""):
    s = traj["summary"]
    log_status("轨迹数据生成", ok=True)
    log_fields(
        里程=f"{s['distance']}m",
        用时=f"{s['duration'] // 60}分{s['duration'] % 60}秒",
        总步数=s["totalStep"],
        平均配速=f"{s['avgPace'] // 60}分{s['avgPace'] % 60}秒/km",
        平均步频=f"{s['avgFrequency']}步/分",
        轨迹点=s["poiCount"],
        步数片=len(traj["stepList"]),
        配速片=len(traj["paceList"]),
        步幅片=len(traj["strideList"])
    )
    # 上传切片是 1000m/片，样本太少时 CV 没有统计意义，直接标出来
    pace_cv = metrics.get("paceCv") if (metrics.get("paceCount") or 0) >= 5 else "切片不足"
    stride_cv = metrics.get("strideCv") if (metrics.get("strideCount") or 0) >= 5 else "切片不足"
    log_fields(
        配速CV=pace_cv,
        步幅CV=stride_cv,
        采样间隔σ=f"{metrics.get('intervalStdMs')}ms",
        精度取值数=metrics.get("accuracyDistinct"),
        稳定度档位=metrics.get("stabilityDistinct")
    )
    if traj["targetPoints"]:
        passed = sum(1 for t in traj["targetPoints"] if t["passStatus"])
        log_fields(打卡点=f"{passed}/{len(traj['targetPoints'])}")
    if dump_path:
        with open(dump_path, "w", encoding="utf-8") as fh:
            json.dump(traj, fh, ensure_ascii=False, indent=2)
        log_status("数据落盘", ok=True, status_text=dump_path)


def _server_target_points(start_data):
    """从起跑响应里取出本单的必经点位，按 sort 排好作为路线控制点。"""
    raw = (start_data or {}).get("targetPoints") or []
    points = []
    for i, p in enumerate(raw):
        try:
            lat, lng = float(p.get("lat")), float(p.get("lng"))
        except (TypeError, ValueError):
            continue
        points.append({
            "code": p.get("code") or i + 1,
            "lat": lat,
            "lng": lng,
            "type": int(p.get("type", 2)),
            "sort": int(p.get("sort") or i + 1),
            "pointName": p.get("pointName") or p.get("simpleName") or "",
        })
    points.sort(key=lambda x: x["sort"])
    return points


def _merge_target_points(server_points, local_points):
    """把本单真实点位与轨迹计算出的通过状态合并，用于上传与结算。"""
    by_code = {str(t.get("code")): t for t in (local_points or [])}
    merged = []
    for p in server_points:
        local = by_code.get(str(p.get("code"))) or {}
        merged.append({
            "code": p["code"],
            "lat": p["lat"],
            "lng": p["lng"],
            "type": p["type"],
            "sort": p["sort"],
            "passStatus": bool(local.get("passStatus")),
            "clockTime": local.get("clockTime") or 0,
        })
    return merged


def _build_traj(args, start_lat, start_lng, waypoints):
    seed = args.seed if args.seed is not None else int.from_bytes(os.urandom(4), "big")
    traj = generate_trajectory_data(
        start_lat,
        start_lng,
        args.distance,
        args.pace,
        waypoints=waypoints,
        shape=args.shape,
        seed=seed,
        step_interval_s=args.step_interval,
        pace_interval_m=args.pace_interval,
        stride_interval_m=args.stride_interval,
        distance_jitter_m=args.distance_jitter,
        pace_jitter_s=args.pace_jitter,
        duration_jitter_s=args.duration_jitter,
        cadence_spm=args.cadence,
    )
    metrics = trajectory_metrics(traj)
    return traj, metrics


# ---------------------------------------------------------------- 业务
def run_sun(client: Any, args, waypoints, start_lat, start_lng) -> int:
    plan_code = args.plan_code
    if not plan_code:
        p_res = client.get_run_plan_list()
        if p_res.get("code") == 0 and (p_res.get("data") or {}).get("list"):
            plan_code = p_res["data"]["list"][0].get("runPlanCode", "")

    log_status("发起阳光跑起跑", ok=True, status_text="提交")
    log_fields(方案代码=plan_code or "未指定", 目标里程=f"{args.distance}m")

    start_res = client.start_sun_run_v2(
        run_plan_code=plan_code,
        target_distance=str(args.distance),
        lng=f"{start_lng:.6f}",
        lat=f"{start_lat:.6f}",
        fence_code=args.fence_code
    )
    if start_res.get("code") != 0:
        log_status("发起阳光跑起跑", ok=False)
        log_error(start_res.get("message", "起跑失败"))
        log_detail(start_res)
        return 1

    start_data = start_res.get("data") or {}
    run_record_code = start_data.get("runRecordCode")
    log_status("发起阳光跑起跑", ok=True)
    log_fields(跑单编号=run_record_code, 检测等级=start_data.get("cheatLevel"),
               打卡模式=start_data.get("clockMode"))

    # 本单的必经点位在起跑响应里就下发了，直接拿来当路线控制点
    server_points = _server_target_points(start_data)
    if server_points:
        waypoints = server_points
        start_lat = float(server_points[0]["lat"])
        start_lng = float(server_points[0]["lng"])
        log_status("本单必经点位", ok=True, status_text=f"{len(server_points)} 个")
        for p in server_points:
            log_fields(**{f"第{p['sort']}点": f"{p.get('pointName')} ({p['lat']:.6f}, {p['lng']:.6f})"})

    traj, metrics = _build_traj(args, start_lat, start_lng, waypoints)
    if server_points:
        traj["targetPoints"] = _merge_target_points(server_points, traj["targetPoints"])
    _print_summary(traj, metrics, args.dump)

    # 1. 轨迹点：按时间切片上传
    chunks = _chunk_by_time(traj["pois"], args.chunk_seconds)
    if not _upload_chunked(
        "轨迹点上传", chunks,
        lambda c: client.upload_run_record(run_record_code, c),
        args.chunk_delay
    ):
        return 1

    # 2. 步数切片
    if not _upload_chunked(
        "步数切片上传", _chunk_list(traj["stepList"], max(1, args.chunk_seconds // 20)),
        lambda c: client.upload_steps_record(run_record_code, c, args.step_interval),
        args.chunk_delay
    ):
        return 1

    # 3. 配速切片
    if not _upload_chunked(
        "配速切片上传", _chunk_list(traj["paceList"], max(1, args.chunk_seconds // 20)),
        lambda c: client.upload_pace_record(run_record_code, c, args.pace_interval),
        args.chunk_delay
    ):
        return 1

    # 4. 必经打卡点
    if traj["targetPoints"]:
        res = client.upload_pass_point(run_record_code, traj["targetPoints"])
        if res.get("code") != 0:
            log_status("打卡点上传", ok=False)
            log_error(res.get("message", "上传失败"))
            log_detail(res)
            return 1
        passed = sum(1 for t in traj["targetPoints"] if t["passStatus"])
        log_status("打卡点上传", ok=True, status_text=f"{passed}/{len(traj['targetPoints'])}")

    # 5. 步幅切片
    if not _upload_chunked(
        "步幅切片上传", _chunk_list(traj["strideList"], max(1, args.chunk_seconds // 20)),
        lambda c: client.upload_stride_record(run_record_code, c, args.stride_interval),
        args.chunk_delay
    ):
        return 1

    # 6. 结算
    s = traj["summary"]
    log_status("提交阳光跑结算", ok=True, status_text="提交")
    log_fields(里程=f"{s['distance']}m", 用时=f"{s['duration']}s")
    fin = client.finish_sun_run_v2(
        run_record_code=run_record_code,
        distance=s["distance"],
        target_points=traj["targetPoints"],
        step_interval=args.step_interval,
        duration=s["duration"],
        pause_count=0,
        pause_times=0,
        total_step=s["totalStep"],
        frequency=s["avgFrequency"],
    )
    if fin.get("code") != 0:
        log_status("提交阳光跑结算", ok=False)
        log_error(fin.get("message", "结算失败"))
        log_detail(fin)
        return 1
    log_status("提交阳光跑结算", ok=True)
    log_detail(fin.get("data"))

    if args.verify:
        _verify(client, run_record_code)
    return 0


def run_free(client: Any, args, waypoints, start_lat, start_lng) -> int:
    log_status("发起自由跑起跑", ok=True, status_text="提交")
    log_fields(目标里程=f"{args.distance}m", 起点纬度=f"{start_lat:.6f}", 起点经度=f"{start_lng:.6f}")

    start_res = client.start_free_run(
        aim_distance=str(args.distance),
        lng=f"{start_lng:.6f}",
        lat=f"{start_lat:.6f}"
    )
    if start_res.get("code") != 0:
        log_status("发起自由跑起跑", ok=False)
        log_error(start_res.get("message", "起跑失败"))
        log_detail(start_res)
        return 1

    run_record_code = (start_res.get("data") or {}).get("runRecordCode")
    log_status("发起自由跑起跑", ok=True)
    log_fields(跑单编号=run_record_code)

    traj, metrics = _build_traj(args, start_lat, start_lng, waypoints)
    _print_summary(traj, metrics, args.dump)

    s = traj["summary"]
    log_status("提交自由跑结算", ok=True, status_text="提交")
    fin = client.finish_free_run(
        run_record_code=run_record_code,
        distance=s["distance"],
        duration=s["duration"],
        total_step=s["totalStep"],
        pois=traj["pois"],
        step_list=traj["stepList"],
        pace_list=traj["paceList"],
        step_interval=args.step_interval,
        pace_interval=args.pace_interval,
    )
    if fin.get("code") != 0:
        log_status("提交自由跑结算", ok=False)
        log_error(fin.get("message", "结算失败"))
        log_detail(fin)
        return 1
    log_status("提交自由跑结算", ok=True)
    log_detail(fin.get("data"))

    if args.verify:
        _verify(client, run_record_code)
    return 0


def _verify(client: Any, run_record_code: str) -> None:
    res = client.run_record_detail(run_record_code)
    if res.get("code") != 0:
        log_status("跑单回查", ok=False)
        log_error(res.get("message", "查询失败"))
        return
    d = res.get("data") or {}
    log_status("跑单回查", ok=True)
    log_fields(
        有效里程=f"{d.get('validDistance', d.get('distance', 0))}m",
        耗时=f"{d.get('duration', 0)}s",
        总步数=d.get("totalStep", 0),
        状态=d.get("status", 0),
        机审异常=d.get("auditDetail", "无")
    )


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(description="长跑协议调度工具")
    parser.add_argument("-t", "--type", choices=["sun", "free"], default="sun", help="跑步类型: sun (阳光跑, 默认), free (自由跑)")
    parser.add_argument("-d", "--distance", type=int, default=2000, help="目标里程 (米)")
    parser.add_argument("-p", "--pace", type=int, default=360, help="平均配速 (秒/公里)")
    parser.add_argument("--plan-code", default="", help="长跑方案编号")
    parser.add_argument("--fence-code", default="", help="打卡电子围栏编号")
    parser.add_argument("--lat", type=float, default=0.0, help="起跑基准纬度")
    parser.add_argument("--lng", type=float, default=0.0, help="起跑基准经度")
    parser.add_argument("--fence", action="append", type=_parse_fence, default=None,
                        help="必经打卡点 CODE,LAT,LNG[,TYPE]，可重复")
    parser.add_argument("--auto-fence", action="store_true", help="从校园围栏接口自动获取打卡点")
    parser.add_argument("--shape", default="auto", choices=["auto", "track", "loop", "grid", "waypoints"],
                        help="路线形态: auto (有打卡点走 waypoints, 否则操场), track, loop, grid, waypoints")
    parser.add_argument("--seed", type=int, default=None, help="随机种子 (同种子可复现)")
    parser.add_argument("--distance-jitter", type=float, default=40.0, help="里程随机上浮 (米)，避免每次都是整数")
    parser.add_argument("--pace-jitter", type=float, default=15.0, help="配速随机浮动 (秒/公里)")
    parser.add_argument("--duration-jitter", type=int, default=6, help="时长随机抖动 (秒)")
    parser.add_argument("--cadence", type=float, default=None, help="平均步频覆盖值 (步/分)，默认自动")
    parser.add_argument("--step-interval", type=int, default=60, help="步数切片间隔 (秒)")
    parser.add_argument("--pace-interval", type=int, default=50, help="配速切片间隔 (米)，服务端上限 100")
    parser.add_argument("--stride-interval", type=int, default=200, help="步幅切片间隔 (米)")
    parser.add_argument("--chunk-seconds", type=int, default=90, help="上传分块对应的轨迹时长 (秒)")
    parser.add_argument("--chunk-delay", type=float, default=1.5, help="分块之间的间隔 (秒)")
    parser.add_argument("--dump", default="", help="把生成的数据包写入 JSON 文件")
    parser.add_argument("--dry-run", action="store_true", help="只生成数据，不发起任何网络请求")
    parser.add_argument("--verify", action="store_true", help="结算后回查跑单详情")
    parser.add_argument("--token", help="临时指定 Token 凭证")
    parser.add_argument("--satoken", help="临时指定 SaToken 凭证")
    parser.add_argument("--transport", default="direct", choices=["direct", "adb", "proxy"], help="网络传输模式")
    parser.add_argument("--proxy", help="代理服务器地址")
    parser.add_argument("--adb-path", help="ADB 命令路径")
    parser.add_argument("--adb-device", help="ADB 目标设备编号")
    args = parser.parse_args()

    waypoints = list(args.fence or [])
    start_lat, start_lng = args.lat, args.lng

    # 纯离线：只生成数据包
    if args.dry_run:
        if not (start_lat and start_lng):
            log_status("载入会话", ok=False)
            log_error("dry-run 需要显式指定起跑坐标：--lat / --lng")
            return 1
        traj, metrics = _build_traj(args, start_lat, start_lng, waypoints)
        _print_summary(traj, metrics, args.dump)
        return 0

    try:
        from client import ShanDongClient
    except Exception as exc:
        log_status("载入会话", ok=False)
        log_error(f"依赖缺失，当前仅可使用 --dry-run：{exc}")
        return 1

    client = ShanDongClient(
        token=args.token,
        satoken=args.satoken,
        transport_mode=args.transport,
        proxy=args.proxy,
        adb_path=args.adb_path,
        adb_device=args.adb_device
    )
    if not client.transport.token:
        log_status("载入会话", ok=False)
        log_error("未检测到有效会话，请先执行 login_cli.py 进行验证")
        return 1

    log_status("载入会话", ok=True)
    log_fields(传输模式=args.transport, 设备机型=client.transport.model_name)

    # 打卡点与起跑坐标的补全：显式 --fence 优先，缺省时才从接口取
    if args.auto_fence:
        fence_points = _fetch_fence_points(client, args.fence_code)
        if fence_points and not waypoints:
            waypoints = fence_points
            log_status("自动获取打卡点", ok=True, status_text=f"{len(waypoints)} 个")
        if fence_points and not (start_lat and start_lng):
            start_lat, start_lng = fence_points[0]["lat"], fence_points[0]["lng"]

    # 围栏编号：没指定就自动取账号所在学校的第一个围栏
    if not args.fence_code:
        try:
            fences = client.query_school_fences().get("data") or []
            if fences:
                args.fence_code = fences[0].get("fenceCode") or ""
                log_status("围栏", ok=True, status_text=f"{fences[0].get('fenceName')} ({args.fence_code})")
        except Exception as exc:
            log_error(f"自动获取围栏失败: {exc}")

    # 起跑坐标：优先命令行，其次围栏中心（起跑响应会把起点覆盖成本单第 1 个点位）
    if not (start_lat and start_lng):
        try:
            fences = client.query_school_fences().get("data") or []
            picked = None
            for f in fences:
                if args.fence_code and f.get("fenceCode") == args.fence_code:
                    picked = f
                    break
            picked = picked or (fences[0] if fences else None)
            if picked:
                start_lat = float(picked.get("lat") or 0)
                start_lng = float(picked.get("lng") or 0)
                log_status("起跑坐标", ok=True, status_text=f"取围栏中心 {picked.get('fenceName')}")
        except Exception as exc:
            log_error(f"读取围栏中心失败: {exc}")

    if not (start_lat and start_lng):
        log_status("起跑坐标", ok=False)
        log_error("缺少起跑坐标，请通过 --lat / --lng 指定，或加 --auto-fence 从围栏接口读取")
        return 1

    # 打卡点过多时按距起跑点由近到远截断，避免路线为了串完所有点而失真
    if len(waypoints) > 8:
        waypoints = sorted(waypoints, key=lambda w: _dist_m(start_lat, start_lng, w["lat"], w["lng"]))[:8]
        log_fields(打卡点="仅取最近 8 个")

    if args.type == "sun":
        return run_sun(client, args, waypoints, start_lat, start_lng)
    return run_free(client, args, waypoints, start_lat, start_lng)


if __name__ == "__main__":
    sys.exit(main())


