# 使用说明

> 本文档以 **8.7.7** 版本为例。其他版本只需把路径中的 `8.7.7` 换成对应版本号，参数完全一致。

---

## 目录

- [一、环境准备](#一环境准备)
- [二、选择客户端版本](#二选择客户端版本)
- [三、首次登录](#三首次登录)
- [四、网络传输模式](#四网络传输模式)
- [五、查询类工具](#五查询类工具)
- [六、跑步流程](#六跑步流程)
- [七、参数详解](#七参数详解)
- [八、判定探针](#八判定探针)
- [九、轨迹自检](#九轨迹自检)
- [十、常见问题](#十常见问题)

---

## 一、环境准备

### 1.1 Python

需要 **Python 3.9 或更高版本**。

```bash
python --version
```

### 1.2 安装依赖

每个版本目录下各有一份 `requirements.txt`，只依赖两个包：

```bash
cd src/8.7.7
pip install -r requirements.txt
```

```
pycryptodome>=3.20.0     # 协议字段加解密
requests>=2.31.0         # HTTP 传输
```

> 建议用虚拟环境，避免污染全局：
> ```bash
> python -m venv .venv
> .venv\Scripts\activate      # Windows
> source .venv/bin/activate   # macOS / Linux
> ```

### 1.3 网络要求

所有请求发往 `https://api.huachenjie.com/run-front`，需要能正常访问该域名。

⚠️ **代码显式禁用了系统代理**（`session.trust_env = False`）。这是有意为之：Clash 之类的代理客户端关闭后，系统代理变量仍在，会导致所有请求连接被拒。**需要代理时必须用 `--proxy` 参数显式指定**，见[第四章](#四网络传输模式)。

---

## 二、选择客户端版本

```
src/
├── 8.7.2/   客户端 8.7.2
├── 8.7.4/   客户端 8.7.4
├── 8.7.7/   客户端 8.7.7（推荐）
└── 8.7.9/   客户端 8.7.9（加壳版，按 8.7.7 维护，待真机验证）
```

**怎么选**：打开手机上的目标 App → 设置 → 关于 → 看版本号，用对应目录。

**各版本不通用**：

- 接口路径、请求参数、加密算法、签名算法、设备标识字段都可能不同
- `session.json` 不跨版本共享（8.7.7 会回退读取 8.7.2 的会话作为兜底）
- **不要把不同版本的代码混着跑**

> 8.7.9 启用了网易易盾加固，APK 内只剩 80KB 壳 dex，业务代码在加密的 `assets/nedata.db` 里。本目录按 8.7.7 的业务面维护，只在 `crypto.py` 更新了 `APP_VERSION` / `BUILD_VERSION`，尚未经过完整真机验证，**优先用 8.7.7**。

---

## 三、首次登录

所有后续命令都要**先 `cd` 进对应版本目录**再执行，脚本用相对路径加载模块。

```bash
cd src/8.7.7
```

### 方式 A：手机号 + 密码

```bash
python login_cli.py -u 13800138000 -p 你的密码
```

执行流程：

1. **前置检查** —— 校验手机号格式与账号状态
2. **密码请求** —— 提交账号密码
3. **滑块人机验证** —— 服务端返回 `1600` 时自动触发：拉取拼图 → 识别缺口偏移 → 校验。识别失败会回退到 `120px` 兜底值重试
4. **凭证核验** —— 请求用户信息接口，解密后打印姓名 / 手机号 / 学校 / 学号

成功后会打印：

```
[✓] 账号登录
[✓] 前置检查
[✓] 人机校验        触发滑块
    偏移量: 87.34px   凭证模式: token
[✓] 凭证核验
    用户ID: xxxxx  姓名: 张*  学校: xx大学
```

凭证写入当前目录的 `session.json`（已在 `.gitignore` 中排除）。

**不想让密码出现在命令行 / 进程列表里**，用环境变量：

```bash
export SHANDONG_PASSWORD='你的密码'      # Windows: set SHANDONG_PASSWORD=你的密码
python login_cli.py -u 13800138000
```

### 方式 B：注入已有 Token

如果已经通过抓包或设备拿到了凭证：

```bash
python login_cli.py --token <token> --satoken <satoken> --device-id <设备码> --model "Android|Pixel 7"
```

参数说明：

| 参数 | 说明 |
|:---|:---|
| `--token` | 主认证 Token |
| `--satoken` | SaToken 会话凭证（部分接口需要） |
| `--device-id` | 16 位十六进制设备识别码，不填会用随机值 |
| `--model` | 机型标识，格式 `Android\|<机型>`，不填用 `Android\|GenericDevice` |

### 查看 / 清除会话

```bash
python login_cli.py          # 不带参数：打印当前已存凭证摘要
python login_cli.py --clear  # 清空 session.json
```

### 会话文件

```json
{
  "token": "...",
  "satoken": "...",
  "device_id": "...",
  "model_name": "Android|GenericDevice",
  "user_id": "...",
  "school_code": "..."
}
```

**不要提交这个文件**，`.gitignore` 已经排除，但自己注意别手动 `git add -f`。

---

## 四、网络传输模式

所有 CLI 都支持三个传输参数：

```bash
--transport {direct,adb,proxy}
```

### direct（默认）

本机直接发起 HTTPS 请求，最简单。

```bash
python run_cli.py -d 2000 -p 360 --lat 36.65 --lng 117.12
```

### adb

请求经 USB 连接的 Android 设备转发，出口 IP 与设备一致。

```bash
python run_cli.py -d 2000 -p 360 \
  --transport adb \
  --adb-path "C:/platform-tools/adb.exe" \
  --adb-device emulator-5554
```

- `--adb-path` 不填时会依次尝试 `ADB_PATH` 环境变量、`PATH` 里的 `adb`
- 都找不到会直接报错：**未找到 adb：请安装 Android Platform Tools，或用 --adb-path / ADB_PATH 指定路径**
- `--adb-device` 不填时用 `adb devices` 的第一个设备

### proxy

```bash
python run_cli.py -d 2000 -p 360 \
  --transport proxy \
  --proxy http://127.0.0.1:7890
```

---

## 五、查询类工具

以下工具都是只读查询，不改动账号数据，可以放心跑。

### 5.1 长跑政策与电子围栏

```bash
python policy_cli.py
```

返回学校的跑步要求（学期目标里程、单次有效里程区间、配速上下限）和校园电子围栏列表。**第一次用建议先跑这个**，拿到围栏编号后再配 `--fence-code`。

### 5.2 用户档案

```bash
python user_cli.py
```

打印用户 ID、姓名、学校、学号、学生卡信息、院系班级。

### 5.3 考核成绩

```bash
python score_cli.py                 # 默认查长跑成绩
python score_cli.py -t all          # 全部：run / fitness / exam / attend
python score_cli.py -s 11           # 指定学期，current 为当前学期
```

### 5.4 跑步历史

```bash
python history_cli.py                        # 阳光跑，最近 10 条
python history_cli.py -t all -s 5            # 全部类型，每页 5 条
python history_cli.py -m 3                   # 最多翻 3 页
python history_cli.py --detail <runRecordCode>   # 单条详情 + 轨迹报告
```

`--detail` 会输出该次跑步的轨迹点、配速曲线、步频，以及**服务端判定结果**，是排查异常跑单的首选工具。

### 5.5 排行榜

```bash
python rank_cli.py
```

### 5.6 每日任务 / 申诉 / 福利

```bash
python task_cli.py --tasks          # 每日任务列表
python task_cli.py --claim-login    # 领取每日登录奖励
python task_cli.py --appeals        # 历史申诉记录
python task_cli.py --jackpot        # 福利大厅抽奖配置
```

### 5.7 其他

| 命令 | 用途 |
|:---|:---|
| `python community_cli.py` | 社区动态、校园新鲜事、帖子浏览 |
| `python message_cli.py` | 闪信与消息通知中心 |
| `python alumni_cli.py` | 本校专区与校友推荐 |
| `python ai_cli.py` | AI 室内运动一键结算 |
| `python face_cli.py` | 人脸识别与底图认证管理 |

---

## 六、跑步流程

**强烈建议先用 `--dry-run` 跑一遍**，确认轨迹数据合理再发起真实请求。

### 6.1 第一步：干跑（不联网）

```bash
python run_cli.py --dry-run -d 2000 -p 360 --lat 36.6512 --lng 117.1201 --dump traj.json
```

输出：

```
[✓] 轨迹数据生成
    目标里程: 2000m   配速: 360s/km   预计用时: 720s
    步数: 1873        步幅: 1.07m
    打卡点: 3/3
[✓] 数据落盘        traj.json
```

打开 `traj.json` 能看到完整的 `pois` / `stepList` / `paceList` / `strideList` / `targetPoints` 四组数据，可以自己核对距离、步数、时长是否自洽。

### 6.2 第二步：正式跑

```bash
python run_cli.py -d 2000 -p 360 --lat 36.6512 --lng 117.1201 --verify
```

执行链路：

```
发起阳光跑起跑  →  跑单编号 runRecordCode
      ↓
分块上传轨迹（默认每 90 秒一块，块间隔 1.5 秒）
      ↓
打卡点上传（逐个上报，记录通过数）
      ↓
提交阳光跑结算  →  里程 / 用时
      ↓
（--verify）回查跑单详情，确认服务端判定结果
```

### 6.3 第三步：核对结果

```bash
python history_cli.py -t sun -s 5
python history_cli.py --detail <上面拿到的 runRecordCode>
```

`--detail` 里重点看是否有异常标记、里程是否被扣减、配速曲线是否被判为不合理。

### 6.4 自由跑

```bash
python run_cli.py -t free -d 2000 -p 360 --lat 36.6512 --lng 117.1201
```

自由跑流程更短：起跑 → 结算，没有分块上传和打卡点。适合不想绑定校园围栏的场景。

### 6.5 自动获取打卡点

不知道围栏编号时，让程序自己拉：

```bash
python run_cli.py -d 2000 -p 360 --auto-fence --lat 36.6512 --lng 117.1201
```

它会先调围栏接口取本校打卡点，再自动规划路线经过这些点。

---

## 七、参数详解

### 7.1 跑步核心参数

| 参数 | 默认值 | 说明 |
|:---|:---|:---|
| `-t, --type` | `sun` | `sun` 阳光跑 / `free` 自由跑 |
| `-d, --distance` | `2000` | 目标里程，单位**米** |
| `-p, --pace` | `360` | 平均配速，单位**秒/公里**。`360` = 6 分钟一公里 |
| `--lat` | `0.0` | 起跑基准纬度 |
| `--lng` | `0.0` | 起跑基准经度 |

> **配速换算**：`-p 300` = 5 分/公里（快跑），`-p 420` = 7 分/公里（慢跑），`-p 600` = 10 分/公里（走路）。
>
> **里程与用时**：用时 = 距离 ÷ 1000 × 配速。`-d 2000 -p 360` → 2km × 6min = 12 分钟。

### 7.2 轨迹形态

| `--shape` | 说明 |
|:---|:---|
| `auto` | 默认，按围栏类型自动选 |
| `track` | 标准跑道，绕圈 |
| `loop` | 环线，闭合路径 |
| `grid` | 街区网格，直角转向 |
| `waypoints` | 严格按指定打卡点串联 |

指定打卡点：

```bash
python run_cli.py -d 2000 -p 360 \
  --shape waypoints \
  --fence C001,36.6512,117.1201 \
  --fence C002,36.6530,117.1225
```

`--fence` 可重复，格式 `编号,纬度,经度`。

### 7.3 数据切片

| 参数 | 默认值 | 说明 |
|:---|:---|:---|
| `--step-interval` | `20` | 步数序列切片间隔（秒） |
| `--pace-interval` | `50` | 配速序列切片间隔（米） |
| `--stride-interval` | `200` | 步幅序列切片间隔（米） |

切片越密，数据包越大、越细腻，但上传耗时也越长。默认值适配大多数学校规则。

### 7.4 上传节奏

| 参数 | 默认值 | 说明 |
|:---|:---|:---|
| `--chunk-seconds` | `90` | 每个上传分块对应的轨迹时长（秒） |
| `--chunk-delay` | `1.5` | 分块之间的真实间隔（秒） |

例如 12 分钟的跑步，按 90 秒分块 = 8 块，每块间隔 1.5 秒，整个过程约 12 秒完成。

> ⚠️ `--chunk-delay` 太小（< 1 秒）容易触发服务端频率限制。默认 1.5 秒是实测比较稳的值。

### 7.5 其他

| 参数 | 说明 |
|:---|:---|
| `--plan-code` | 长跑方案编号，不填由服务端分配 |
| `--fence-code` | 打卡电子围栏编号 |
| `--seed` | 随机种子，同种子生成完全一致的轨迹，便于复现问题 |
| `--dump <file>` | 把生成的数据包写入 JSON 文件 |
| `--dry-run` | 只生成数据，**不发任何网络请求** |
| `--verify` | 结算后回查跑单详情 |

---

## 八、判定探针

`risk_probe.py` 用来确认"这单会不会被服务端判异常"，代价比正式跑小。

### 8.1 只读规则（最安全）

```bash
python risk_probe.py --config-only
```

只拉取服务端的判定规则配置，不起跑、不产生跑单。能拿到：

- 9 类作弊标记的启用状态与阈值
- 配速上下限、步频合理区间
- 人脸校验触发规则
- 单次有效里程区间

### 8.2 回查已有跑单

```bash
python risk_probe.py --detail <runRecordCode>
```

分析指定跑单被判在哪一层，输出比 `history_cli.py --detail` 更详细。

### 8.3 最小里程实跑

```bash
python risk_probe.py --lat 36.65 --lng 117.12 -d 800 -p 420 --settle-wait 8 --clean
```

| 参数 | 说明 |
|:---|:---|
| `-d` | 默认 800 米，比正式跑短，代价小 |
| `--settle-wait` | 结算后等待机审结果秒数，默认 5 |
| `--clean` | 失败时调用 `abnormalFinishSunRun` 清理挂单，避免留下半截记录 |

---

## 九、轨迹自检

不联网，纯算法验证：

```bash
python traj_selftest.py
```

检查项：

- 距离、步数、时长三组数据的交叉校验关系是否自洽
- 配速曲线是否落在合理区间
- 坐标点是否连续、无跳变
- 统计分布是否符合真实跑步特征

改动 `running_protocol.py` 之后**必跑**这个，能挡掉大部分"数据一看就假"的问题。

---

## 十、常见问题

<details>
<summary><b>登录返回 1600，一直卡在滑块？</b></summary>

`1600` 表示触发人机验证，脚本会自动走滑块识别。如果反复失败：

1. 降低识别精度要求：脚本已经有两种凭证模式自动重试，都失败说明缺口识别偏差过大
2. 换个时段再试，同一手机号短时间内频繁请求会被加强校验
3. 直接走[方式 B](#方式-b注入已有-token) 注入 Token
</details>

<details>
<summary><b>提示"未找到 adb"？</b></summary>

代码里**不再内置任何硬编码的 adb 路径**。三种解决办法：

```bash
# 1. 装 Platform Tools 并加进 PATH
# 2. 显式指定
python run_cli.py --transport adb --adb-path "D:/platform-tools/adb.exe"
# 3. 设环境变量
set ADB_PATH=D:\platform-tools\adb.exe
```
</details>

<details>
<summary><b>请求全部失败 / 连接被拒？</b></summary>

代码主动忽略了系统代理（`trust_env = False`）。如果你的机器上开着 Clash 之类的代理软件：

```bash
# 要么关掉代理
# 要么显式指定
python xxx_cli.py --transport proxy --proxy http://127.0.0.1:7890
```
</details>

<details>
<summary><b>跑完里程被扣了 / 显示异常？</b></summary>

先用探针定位：

```bash
python risk_probe.py --detail <runRecordCode>
python history_cli.py --detail <runRecordCode>
```

常见原因：

- 配速超出学校规则区间（太快或太慢）
- 步频/步幅与配速不匹配
- 轨迹点跨度过大，被判定为跳跃
- 打卡点未全部通过

调整 `--pace`、`--shape`，或先跑 `--config-only` 看本校的具体阈值。
</details>

<details>
<summary><b>换版本后报签名错误 / 接口 404？</b></summary>

客户端版本和脚本版本不匹配。打开 App 看版本号，用对应目录。如果 App 刚刚更新、仓库里还没有对应目录，需要等协议适配。
</details>

<details>
<summary><b>session.json 能不能提交 / 分享？</b></summary>

**不能。** 里面是你的 token 和用户 ID，等同于账号凭证。`.gitignore` 已排除，但别手动 `git add -f`。
</details>

---

## 免责声明

本项目用于 Android 协议分析、逆向工程与密码学算法研究，属于技术学习范畴。

使用者需自行承担全部后果，包括但不限于账号处罚、成绩作废、纪律处分。作者不对任何使用行为负责，也不提供任何规避检测的保证。

**请勿将本项目用于违反所在学校规定或平台服务条款的用途。**
