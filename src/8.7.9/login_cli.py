"""登录与凭证管理命令行工具。"""
import argparse
import os
import sys

cur_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, cur_dir)

import crypto
import captcha_solver
from client import ShanDongClient
from logger import log_status, log_fields, log_error, log_detail


def _solve_captcha(client: ShanDongClient, y_mode: str):
    """拉一次验证码、识别缺口、提交校验。

    y_mode: "fixed5" 固定 5.0 / "piece" 用拼图块自身的纵坐标 / "fixed15" 固定 15.0
    返回 (ok, v_res, raw_x, secret_key, score)
    """
    import requests

    c_res = client.get_slider_captcha()
    if c_res.get("code") != 0 or not c_res.get("data"):
        log_status("拉取验证码", ok=False)
        log_error(c_res.get("message", "拉取失败"))
        return False, None, 0.0, None, 0.0

    cdata = c_res["data"]
    token = cdata.get("token") or ""
    secret_key = cdata.get("secretKey") or ""
    bg_url = cdata.get("originalImageUrl")
    tmpl_url = cdata.get("jigsawImageUrl")

    if not secret_key:
        log_error("验证码响应里没有 secretKey，无法加密点位")
        return False, c_res, 0.0, None, 0.0

    try:
        bg_bytes = requests.get(bg_url, timeout=10, verify=False).content
        tmpl_bytes = requests.get(tmpl_url, timeout=10, verify=False).content
    except Exception as exc:
        log_status("下载验证码图片", ok=False)
        log_error(str(exc))
        return False, c_res, 0.0, secret_key, 0.0

    raw_x, score, meta = captcha_solver.solve_slider_notch_debug(bg_bytes, tmpl_bytes)
    piece_y = meta.get("bbox", (0, 0))[1] if meta.get("bbox") else 5
    y = {"fixed5": 5.0, "piece": float(piece_y), "fixed15": 15.0}.get(y_mode, 5.0)

    log_status("识别缺口", ok=True)
    log_fields(偏移量=f"{raw_x:.1f}px", 置信度=meta.get("score"), 纵坐标=y, 模式=y_mode)

    v_res, _ = client.verify_slider_captcha(token, raw_x, secret_key=secret_key, y=y)
    passed = bool(v_res.get("code") == 0 and (v_res.get("data") or {}).get("result") is True)
    return passed, v_res, raw_x, secret_key, score


def main():
    parser = argparse.ArgumentParser(description="闪动校园登录与凭证管理")
    parser.add_argument("-u", "--mobile", help="登录手机号")
    parser.add_argument("-p", "--password", help="登录密码")
    parser.add_argument("--token", help="直接注入 Token 凭证")
    parser.add_argument("--satoken", help="直接注入 SaToken 凭证")
    parser.add_argument("--device-id", help="设备识别码")
    parser.add_argument("--model", help="机型标识")
    parser.add_argument("--clear", action="store_true", help="清除本地保存的会话")
    parser.add_argument("--transport", default="direct", choices=["direct", "adb", "proxy"], help="网络传输模式")
    parser.add_argument("--proxy", help="代理服务器地址")
    parser.add_argument("--adb-path", help="ADB 命令路径")
    parser.add_argument("--adb-device", help="ADB 目标设备编号")
    args = parser.parse_args()

    client = ShanDongClient(
        transport_mode=args.transport,
        proxy=args.proxy,
        adb_path=args.adb_path,
        adb_device=args.adb_device
    )

    if args.clear:
        client.clear_saved_session()
        log_status("清除本地会话", ok=True)
        return

    # 直接注入 Token 模式
    if args.token:
        client.transport.token = args.token
        if args.satoken:
            client.transport.satoken = args.satoken
        if args.device_id:
            client.transport.device_id = args.device_id
        if args.model:
            client.transport.model_name = args.model
        client._save_current_session()
        log_status("注入凭证", ok=True)
        log_fields(Token=args.token[:12] + "...", 状态="已持久化")

        info_res = client.query_common_user_info()
        if info_res.get("code") == 0 and info_res.get("data"):
            udata = info_res["data"]
            client.user_id = udata.get("userId")
            client.school_code = udata.get("schoolCode")
            client._save_current_session()
            log_status("凭证核验", ok=True)
            log_fields(
                用户ID=udata.get("userId"),
                姓名=crypto.decrypt_field(udata.get("userName", "")),
                手机号=crypto.decrypt_field(udata.get("phone", "")),
                学校=crypto.decrypt_field(udata.get("schoolName", "")),
                学号=crypto.decrypt_field(udata.get("studentNumber", ""))
            )
        else:
            log_status("凭证核验", ok=False)
            log_error(info_res.get("message", "核验失败"))
            log_detail(info_res)
        return

    # 密码也可以通过环境变量传入，避免出现在命令行 / 进程列表里
    password = args.password or os.environ.get("SHANDONG_PASSWORD") or ""

    if not args.mobile or not password:
        if client.transport.token:
            log_status("已存会话", ok=True)
            log_fields(
                Token=client.transport.token[:16] + "...",
                SaToken=client.transport.satoken[:16] + "..." if client.transport.satoken else "无",
                设备=client.transport.model_name,
                用户ID=client.user_id or "未记录"
            )
        else:
            parser.print_help()
        return

    log_status("账号登录", ok=True, status_text="开始")
    log_fields(账号=args.mobile, 模式="密码与滑块验证", 密码来源=("参数" if args.password else "环境变量"))

    # 1. 登录前置检查
    chk_res = client.login_check(args.mobile)
    if chk_res.get("code") != 0:
        log_status("前置检查", ok=False)
        log_error(chk_res.get("message", "前置检查失败"))
        log_detail(chk_res)
        return

    log_status("前置检查", ok=True)

    # 2. 初次密码请求
    l_res = client.login_by_password(args.mobile, password)

    # 3. 滑块：点位用本次 secretKey 加密，纵横坐标模式逐个尝试
    if l_res.get("code") == 1600:
        log_status("人机校验", ok=True, status_text="触发滑块")
        # /captcha/check 的 result 恒为 false（实测对垃圾数据也返回 false），
        # 不能拿它当判据，直接拿点位去登录，用登录返回码判断。
        last_verify = None
        attempts = [("点位原值", 0.0, "fixed5"), ("点位+10", 10.0, "fixed5")]
        for label, dx, y_mode in attempts:
            _ok, v_res, raw_x, secret_key, _score = _solve_captcha(client, y_mode)
            last_verify = v_res
            if not secret_key:
                log_error("没有 secretKey，跳过")
                continue
            point = crypto.encrypt_slider_point(raw_x + dx, y=5.0, key=secret_key)
            log_fields(尝试=label, 点位=f"{raw_x + dx:.1f}", 模式=y_mode)
            l_res = client.login_by_password(args.mobile, password, captcha_point=point)
            log_status("账号登录", ok=(l_res.get("code") == 0), status_text=str(l_res.get("code")))
            if l_res.get("code") == 0:
                break
            log_error(l_res.get("message", "登录失败"))
            if l_res.get("code") != 1601:
                break

        if last_verify is not None:
            log_status("验证码响应原文", ok=True)
            log_detail(last_verify)

    if l_res.get("code") == 0 and l_res.get("data"):
        data = l_res["data"]
        log_status("账号登录", ok=True)
        log_fields(
            用户ID=data.get("userId"),
            Token=(data.get("token") or "")[:16] + "...",
            SaToken=(data.get("satoken") or "")[:16] + "...",
            学校代码=data.get("schoolCode")
        )
        if data.get("alertTip"):
            tip = data["alertTip"]
            log_fields(提示=f"{tip.get('title')}: {tip.get('content')}")
    else:
        log_status("账号登录", ok=False)
        log_error(l_res.get("message", "登录失败"))
        log_detail(l_res)


if __name__ == "__main__":
    main()
