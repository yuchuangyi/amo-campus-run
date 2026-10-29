# 跑单检测判定 · 测试方案（8.7.9）

> 目的：用最小代价确认「协议复现的跑单」会不会被服务端判定异常，以及被判在哪一层。
> 工具：`8.7.9/risk_probe.py`（判定探针）、`8.7.9/traj_selftest.py`（本地统计自检）
> 依据：8.7.7 真实 dex + 8.7.9 APK 资源表（两版业务面一致，见文末）

---

## 一、服务端能看到的判定面

| 层 | 服务端依据 | 对应字段 |
|---|---|---|
| 轨迹统计 | 采样间隔方差、精度/稳定度方差、方位角平滑度、配速/步幅方差 | `pois` `paceList` `strideList` |
| 时序 | 五个上传接口的到达跨度、`collectTime` 与接收时间的关系、`duration` 与实际经过时间 | 服务端日志 |
| 打卡 | 必经点位是否经过、顺序是否正确 | `targetPoints.passStatus` |
| 人脸 | 配置要求校验 N 次、成功 M 次 | `faceCheckRecordList` / `faceCheckInfoList` |
| 环境 | 9 类客户端标记：adb / 模拟器 / magisk / 多开 / 代理 / 第三方 ROM / root / 签名 / fakeLocation | 启动与结算时的设备画像 |
| 账号 | 挂单率、短时间多次完赛、设备与 IP 归属地不一致 | 账号维度统计 |

其中「环境」标记来自枚举 `com.huachenjie.common.bean.CheatType`，共 **11 个**字段
（从 8.7.7 真实 dex 反编译取得）：

```
ADB  BD_MOCK  EMULATOR  HOOK  MAGISK  MULTI_APP
PROXY  ROOT  SIGN  THIRD_ROM  VIRTUAL_LOCATION
```

资源表里能对上中文文案的是 9 个（`HOOK`、`BD_MOCK` 复用了别的提示文案）：

```
设备连接Adb / 模拟器运行 / Magisk+(%s) / 应用被多开 / 使用网络代理
第三方ROM+(%s) / 设备已root / 签名被篡改 / 使用fakeLocation
```

人脸规则来自 `run_face_check_rule_*`：

```
按学校要求需校验 %1$d 次，并保证 %2$d 次校验成功，人脸校验结果才算通过。
人脸校验结果不通过，会导致本次跑步成绩无效。
```

> 结论先行：纯协议客户端**不带**摄像头、**不带**环境画像，轨迹与时长可以做到自洽，
> 但人脸校验与环境画像是两个绕不开的洞 —— 除非学校的方案里这两项没开。

---

## 二、分级测试流程

风险从 0 到 1，每级都能独立停下来。

### L0 只读规则（零痕迹，不产生跑单）

```bash
python risk_probe.py --config-only
```

看三件事：

1. 方案里 `targetDistance` 多少、有没有配速上下限
2. 有没有 `faceCheckCount` / `faceCheckPassCount`（大于 0 就是硬门槛）
3. 有没有围栏/打卡点要求

**这一步同时是 8.7.9 的协议兼容性验证**：请求会带 `appVersion=8.7.9`、
`buildVersion=26101910` 和新签名。如果返回签名错误 / 版本不支持 / 参数解密失败，
说明 8.7.9 换了签名或字段加密方案，后面几级先别做，得回去补静态分析。

**这一步就能判定值不值得继续。**

### L1 本地自检（不联网）

```bash
python traj_selftest.py
```

确认生成的轨迹在统计层面站得住（配速 CV、步幅 CV、采样间隔σ、方位角σ 都在真人区间）。
这一级不过，后面不用跑。

### L2 单次最小里程实跑

```bash
python risk_probe.py --lat <纬度> --lng <经度> -d 800 -p 420 --clean
```

- 800 米、7'00"/km（慢速、低强度）
- `--clean`：失败时自动 `abnormalFinishSunRun`，不留挂单
- 跑完自动回查 `runRecordDetail` 并打印判定

### L3 加量

单次通过后，再逐项加：

1. 里程 800 → 1600 → 2000
2. 配速 7'00" → 6'00"
3. 连续天数（第 1 天、第 2 天、第 3 天各观察一次）
4. 换设备指纹（`deviceId` / 机型）看是否触发「多设备」

### L4 只回查已有跑单

```bash
python risk_probe.py --detail <runRecordCode>
```

用于观察机审是否有延迟（有些判定 T+1 才落库）。

---

## 三、结果判读

`risk_probe.py` 已经把这些映射好了，人肉看的话：

| 现象 | 含义 | 处理 |
|---|---|---|
| `status` 1/2 且 `validDistance` ≈ 目标 | 未被判定 | 可以继续加量 |
| `status` 1/2 但 `validDistance` 明显变小 | 被按比例扣里程 | 轨迹统计或打卡有问题 |
| `status` 3 | 作弊/无效 | 看 `cheatType` 与环境画像 |
| `status` 4 | 机审未通过 | 看 `auditDetail` 的命中项 |
| `auditDetail` 非空 | 机审命中了具体规则 | 按命中项反推 |
| `faceCheckInfoList` 非空且未通过 | 人脸缺口 | 需要真实人脸流程 |
| `targetPoints` 里有 `passStatus=false` | 必经点没走 | 路线要穿过打卡点 |

---

## 四、最容易先撞上的三堵墙

1. **人脸校验**：只要学校开了 `faceCheckCount > 0`，空 `faceCheckRecordList` 基本进不了「通过」
2. **环境画像**：协议客户端上报的设备信息与真机 App 不一致（没有易盾 token、没有传感器/步数来源）
3. **时序**：数据是事后补的，`collectTime` 与接口到达时间的关系、五接口到达跨度都能被看到

---

## 五、8.7.9 相对 8.7.7 的变化（静态核验）

| 项 | 8.7.7 | 8.7.9 |
|---|---|---|
| versionName / versionCode | 8.7.7 / 26101017 | 8.7.9 / **26101910** |
| dex 数量 | 23 个（136.8 MB） | **1 个（80 KB 壳）** |
| 业务代码位置 | APK 内 | `assets/nedata.db`（52 MB，熵 8.0，加密） |
| 加固壳 | 未加固 | **网易易盾** `com.netease.nis.wrapper.*` + `libnesec.so` / `liby.so` |
| 资源名（作弊类型、人脸规则） | 9 类 / 5 条 | **完全一致** |
| Flutter `libapp.so` | — | **与 8.7.7 同 hash** |
| 组件与界面 | — | 基本一致（`sun_run` 包改名 `sunshine_run`） |

**推论**：8.7.9 是「同业务逻辑 + 加壳」的版本，协议面大概率未变；
`crypto.py` 的 `APP_VERSION` / `BUILD_VERSION` 需要换成 `8.7.9` / `26101910`（已落在 `8.7.9/`）。

**未能静态核验的部分**：真实 dex 在加密容器里，接口表、签名逻辑是否有微调，需要
运行时 dump 或一次真机抓包才能确认。这也是 `8.7.9/` 目录标注「待真机验证」的原因。
