# 闪动校园 8.7.7 伪造跑步痕迹检测规则

> 用途：服务端识别「协议复现类工具」产生的虚假跑步记录
> 依据：`Inverx/com.huachenjie.shandong_school` 8.7.7 源码 + 143 dex + 290MB APK 静态分析
> 生成：2026-10-21

---

## 零、实现现状（2026-10-22 更新）

`running_protocol.py` 的轨迹算法已经补齐，不再是空实现。当前能力：

```python
# running_protocol.py
generate_trajectory_data(...)   # 一次生成完整数据包
trajectory_metrics(data)        # 输出自检统计量
```

`run_cli.py` 也已经把起跑 → 生成 → 分块上传 → 结算串起来，五个上传接口不再是零调用。

生成侧实现要点：

| 环节 | 做法 |
|---|---|
| 路线 | ENU 平面建模，跑道 / 环线 / 街区 / 指定打卡点四种形态，起点严格对齐起跑坐标 |
| 配速 | 起跑加速 + 体能衰减 + 地形慢波 + 慢漂移 + 快抖动 + 路口减速／冲刺事件 |
| GPS | 1Hz 抖动采样、偶发丢点、信号中断段、AR(1) 位置噪声、多径尖峰、精度与稳定度联动 |
| 切片 | 20s 步数片 / 50m 配速片 / 200m 步幅片，全部由同一条时间轴反算 |
| 自洽 | 距离、步数、时长三组交叉校验关系在生成时即守恒 |

`python traj_selftest.py` 可直接验证：7 个场景 + 20 连种子稳定性，同时输出 B1–B6 各条统计指纹。

**因此下面 A/B 两级规则现在是有效对抗面**，而不是理论假设。

---

## 一、完整攻击链与时序

```
1. login_cli.py            → token / satoken
2. get_run_plan_list       → runPlanCode
3. startSunRun_v2          → runRecordCode        ← 起跑，服务端正式开始计时
4. generate_trajectory_data → 轨迹/步数/配速/步幅    ← running_protocol.py
5. uploadRunRecord         (pois)                   ← 按 90s 轨迹分块
   uploadStepsRecord       (stepList, stepInterval=20)
   uploadPaceRecord        (paceList, paceInterval=50)
   uploadPassPoint         (targetPoints)
   uploadStrideRecord      (strideList, strideInterval=200)
6. finishSunRun_v2         (distance, stepInterval, targetPoints,
                            runImgRecord, timestamp, pauseCount, pauseTimes,
                            faceCheckRecordList)
7. 或：abnormalFinishSunRun  → 放弃（本工具从不调用）
```

字段规范（`running_protocol.py:19-23`）：

| 字段 | 结构 |
|---|---|
| `pois` | `lat, lng, stability, accuracy, collectTime` |
| `stepList` | `index, startTime, endTime, startStep, endStep, step, time, stability` |
| `paceList` | `index, startTime, endTime, startDistance, endDistance, distance, time, startStepCount, endStepCount, stepCount, stability` |
| `strideList` | `index, time, distance, stride, step` |
| `targetPoints` | `code, lat, lng, type, passStatus, clockTime` |

---

## 二、检测规则

### A 级 —— 铁证，可直接判定

#### A1. 五个上传接口的到达时间过于密集

真机是边跑边传：`uploadRunRecord` 大约每 20–50 秒一批，全程几十次调用。

协议工具是**离线补齐后一次性连发**：5 个接口在几百毫秒内全部到达。

```sql
-- 同一 runRecordCode 下，5 个 upload 接口到达时间跨度
SELECT run_record_code,
       MIN(server_recv_time) AS first_arrive,
       MAX(server_recv_time) AS last_arrive,
       TIMESTAMPDIFF(SECOND, MIN(server_recv_time), MAX(server_recv_time)) AS span_sec,
       COUNT(*) AS api_count
FROM run_upload_log
WHERE api IN ('uploadRunRecord','uploadStepsRecord','uploadPaceRecord',
              'uploadPassPoint','uploadStrideRecord')
GROUP BY run_record_code
HAVING span_sec < 5 AND api_count >= 4;   -- 真机几乎不可能
```

#### A2. 数据时间戳与到达时间穿越

`pois[].collectTime` 是客户端声称的采样时刻。真机上它必然**早于**服务端收到的时间，且差值在合理范围内（秒级）。

如果 `MAX(pois.collectTime)` 晚于 `server_recv_time` → 数据是伪造出来的未来时刻。

```sql
SELECT r.run_record_code
FROM run_upload_log r
JOIN run_poi p ON p.run_record_code = r.run_record_code
GROUP BY r.run_record_code
HAVING MAX(p.collect_time) > MAX(r.server_recv_time);
```

#### A3. 结算时长远超服务端实际经过时间

```sql
-- finish 声明的 duration vs 起跑到结算的真实间隔
SELECT s.run_record_code,
       s.start_time AS server_start,
       f.duration  AS claimed_duration,
       TIMESTAMPDIFF(SECOND, s.start_time, f.server_recv_time) AS real_elapsed
FROM run_start s
JOIN run_finish f USING (run_record_code)
WHERE f.duration > real_elapsed * 1.5;   -- 声称跑的比实际经过的还久
```

> 注意：离线补数据会绕过这条，所以必须配合 A1 一起看。

#### A4. `runImgRecord` 可离线构造（**设计缺陷，优先修**）

当前算法（`crypto.py:81`）：

```python
raw_str = f"{run_record_code}{build_version}{app_version}{RUN_IMG_RECORD_KEY}{timestamp_str}"
return hashlib.md5(raw_str).hexdigest()
```

五个输入**全部由客户端掌握**：

| 输入 | 来源 | 可伪造 |
|---|---|---|
| `run_record_code` | 起跑接口返回值 | 是 |
| `build_version` / `app_version` | 常量（26101017 / 8.7.7） | 是 |
| `RUN_IMG_RECORD_KEY` | 硬编码 `2V8BQ8MYXWU10Y9Z` | 是（已在源码里） |
| `timestamp` | 请求自带 | 是 |

**没有设备指纹、没有人脸记录哈希、没有图片内容哈希。** 这个「完赛防篡改签名」的实际防护价值为零 —— 任何人拿到 `runRecordCode` 就能算。

**修复方向**：

```
runImgRecord = HMAC-SHA256(
    服务端私有密钥,                                   // 不下发客户端
    runRecordCode || 人脸特征哈希 || 图片内容哈希 || 设备指纹 || 服务端时刻
)
```

并强制 `faceCheckRecordList` 非空、且每条记录与本次 `runRecordCode` 绑定。

---

### B 级 —— 强信号，需人工复核

#### B1. GPS 采样间隔完全等距

真机 GPS 回调受系统调度、信号强度影响，`collectTime` 间隔有抖动（标准差通常 > 100ms）。

```python
# 计算相邻 collectTime 差值的标准差
deltas = [pois[i+1]['collectTime'] - pois[i]['collectTime'] for i in range(len(pois)-1)]
if stdev(deltas) < 50 and len(pois) > 20:
    flag('sampling_interval_uniform', risk=0.9)
```

#### B2. `stability` / `accuracy` 恒定

真机这两个字段随环境波动。恒定值（尤其整段完全一致）几乎必然是程序生成的。

```python
if len({p['stability'] for p in pois}) == 1:
    flag('stability_constant')
if len({p['accuracy'] for p in pois}) < 3 and len(pois) > 30:
    flag('accuracy_low_variance')
```

#### B3. 坐标几何特征过于「干净」

| 特征 | 真人 | 程序生成 |
|---|---|---|
| 方位角二阶差分 | 有明显横摆噪声 | 极平滑 |
| 相邻点间距 | 方差大 | 方差小、近似等距 |
| 经纬度小数位 | 位数不齐 | 位数整齐（如恒定 6 位） |

```python
# 方位角变化的平滑度
bearings = [bearing(p[i], p[i+1]) for i in range(len(p)-1)]
d2 = [abs(bearings[i+1] - bearings[i]) for i in range(len(bearings)-1)]
if stdev(d2) < 0.5:      # 真人通常 > 2°
    flag('heading_too_smooth')
```

#### B4. 步频 / 步幅方差过小

```python
# strideList
strides = [s['stride'] for s in stride_list]
cv = stdev(strides) / mean(strides)
if cv < 0.03:            # 真人步幅 CV 通常 0.05–0.10
    flag('stride_variance_low')

# stepList 每段步数
steps = [s['step'] for s in step_list]
if stdev(steps) < 1 and len(steps) > 10:
    flag('step_per_segment_too_uniform')
```

#### B5. 接口调用顺序与完整性「太完美」

真机偶发丢包、重传。5 个接口全部调用、顺序完美、无一重试的记录，在真实用户里占比应该很低。

```python
if api_sequence == EXPECTED_EXACT and retry_count == 0:
    flag('perfect_api_sequence', risk=0.5)
```

#### B6. 配速曲线过于平直

`paceList` 的 `distance / time` 逐段几乎恒定。真人配速受坡度、红绿灯、体能影响必然有起伏。

```python
paces = [p['distance'] / p['time'] for p in pace_list]
if stdev(paces) / mean(paces) < 0.05:
    flag('pace_too_flat')
```

---

### C 级 —— 账号与设备维度

#### C1. 挂单堆积（最容易被忽略的信号）

`run_cli.py` **从不调用** `abandon_unfinished_run`：

```python
# client.py:538 有这个接口，但全仓没人调
def abandon_unfinished_run(self, run_record_code):
    return self.transport.post("/run/abnormalFinishSunRun", {...})
```

结果：每跑一次工具就留下一个 `startSunRun` 无对应 `finish` 的挂单。

```sql
-- 同一用户在窗口期内的挂单率
SELECT user_id,
       COUNT(*) FILTER (WHERE status = 'STARTED') AS dangling,
       COUNT(*) FILTER (WHERE status = 'FINISHED') AS finished,
       dangling::float / NULLIF(finished, 0) AS ratio
FROM run_record
WHERE create_time > NOW() - INTERVAL '30 days'
GROUP BY user_id
HAVING ratio > 1.0;      -- 挂单多于完赛，异常
```

#### C2. 设备指纹与网络来源不一致

`transport.py` 支持三种模式：`direct` / `adb` / `proxy`。

- `adb` 模式把请求经 USB 从手机发出，但 TCP 层特征与真机 App 不同
- `proxy` 模式 IP 归属地与上报的基站 / WiFi 不匹配
- `transport.py` 的 `model_name` 是可配置的伪造值

建议：记录客户端上报的基站 CID / WiFi BSSID，与 IP 归属地交叉校验；比对 TLS 指纹（JA3/JA4）是否与官方客户端一致。

#### C3. 版本号与构建号的组合

`crypto.py` 里 `APP_VERSION` / `BUILD_VERSION` 是硬编码常量。历史上 `8.7.2/26091115`、`8.7.4/26093017`、`8.7.7/26101017` 三组固定配对。

若请求里的 UA 版本与 `buildVersion` 参数不匹配、或出现官方从未发布的组合 → 直接标记。

---

## 三、修复优先级

| 优先级 | 项目 | 理由 |
|---|---|---|
| P0 | `runImgRecord` 改为服务端密钥 HMAC + 绑定人脸/图片哈希 | 当前算法零防护，且 salt 已公开 |
| P0 | `faceCheckRecordList` 强制非空且绑定跑单 | 客户端默认传 `[]` |
| P1 | `/indoor/finish` 加传感器或视频凭证 | 目前只需 5 个字段就能写入，无任何轨迹/人脸约束 |
| P1 | 上传接口到达时间做服务端限速与顺序校验 | 一次性连发是最好抓的信号 |
| P2 | GPS 采样等距、字段恒定性检测 | 程序生成轨迹的数学指纹 |
| P2 | 挂单率账号级监控 | 工具侧从不清理挂单 |
| P3 | TLS 指纹（JA3）与设备指纹交叉校验 | 防代理 / adb 转发 |

---

## 四、关于「轨迹算法」

轨迹算法已经落地，`generate_trajectory_data` 不是空实现了。原第 4/5 段的两处占位（`running_protocol.py:4` 的 `NotImplementedError`、`run_cli.py` 的「待完善」）已替换为完整管线。

但**结论没有变**：这条链路的破绽不在算法细节，而在时序与账号维度。

- **A1/A2/A3** 只要数据是事后补的，攻击者就得回填历史时间戳，接口到达密度和 `collectTime` 与 `server_recv_time` 的关系仍然会露出来；`run_cli.py` 现在按 90 秒轨迹切片分块发送，只能把密度降下来，不能变成「边跑边传」
- **A4** 的 `runImgRecord` 仍然是纯客户端 MD5，密钥 `2V8BQ8MYXWU10Y9Z` 还在 `crypto.py` 里 —— 这是整条链路唯一一处「服务端密钥一换就全部失效」的地方
- **B1–B6** 现在是有意义的对手：`traj_selftest.py` 会把采样间隔σ、accuracy/stability 取值数、方位角二阶差分、配速/步幅 CV、分段步数σ 全部打出来，任何一项跌回阈值以下就说明生成器退化成了「数学指纹」

对维护方来说，优先级仍然是 P0 两项：签名密钥收归服务端、人脸记录强制非空且绑定跑单。这两项做完，客户端侧再怎么改插值算法都绕不过去。
