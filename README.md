# 闪动校园协议实现

<p align="center">
  <b>Shandong School · Android Reverse Engineering & Protocol Analysis</b>
</p>

<p align="center">
  闪动校园 Android 客户端协议分析与 Python 协议实现，按客户端版本独立维护。
</p>

---

## 项目简介

本项目主要用于 **闪动校园 Android 客户端** 的逆向分析、网络协议分析及协议工程化研究。

当前项目仅维护：

```text
Android Reverse Engineering
Network Protocol Analysis
Protocol Reconstruction
Cryptographic Algorithm Analysis
Device Identifier Analysis
Python Protocol Implementation
```

项目按照客户端版本独立维护，不同版本之间尽量保持解耦，主要用于协议分析、版本差异研究及相关技术交流。

历史模块方向属于早期探索内容，后续不再维护、更新或适配。当前仓库仅维护协议实现及相关研究内容。

当前版本：

```text
8.7.2
8.7.4
8.7.7
8.7.9
```

后续将根据客户端版本更新情况继续进行协议分析与适配。

---

## 项目结构

```text
amo-campus-run/
│
├── src/
│   ├── 8.7.2/   协议实现
│   ├── 8.7.4/   协议实现
│   ├── 8.7.7/   协议实现
│   └── 8.7.9/   协议实现（加壳版，待真机验证）
│
├── docs/
│   ├── USAGE.md                    完整使用说明（从这里开始）
│   ├── reverse-analysis-8.7.7.md   逆向分析报告
│   ├── reverse-analysis-8.7.9.md   静态逆向报告
│   ├── detection-rules-8.7.7.md    伪造痕迹检测规则
│   ├── detection-test-plan.md      检测判定测试方案
│   └── detection-results-8.7.9.md  实跑检测结论
│
├── .tools/      设备调试脚本（adb / 登录探测 / 单次运行）
├── README.md
└── .gitignore
```

不同客户端版本可能存在：

```text
接口
请求参数
加密算法
签名算法
设备标识
数据结构
服务端校验逻辑
```

等变化。

因此各版本协议独立维护，请根据实际客户端版本选择对应目录。

---

## 技术方向

本项目主要涉及：

```text
Android Reverse Engineering
        ↓
Traffic Capture
        ↓
Protocol Analysis
        ↓
Device Identifier Analysis
        ↓
Native / Java Logic Analysis
        ↓
Cryptographic Reconstruction
        ↓
Protocol Reconstruction
        ↓
Python Implementation
```

重点研究客户端与服务端之间的：

```text
Request
Response
Authentication
Device Identifier
Signature
Encryption
Data Structure
Protocol Flow
```

等内容。

---

## 设备标识采集

### 非 Root Android 设备

通过修改闪动校园客户端测试包，在非 Root Android 设备上采集和观察客户端实际使用的设备标识。

主要研究内容：

- 客户端设备标识的采集与分析
- 设备标识相关字段的来源及使用方式
- 客户端设备标识与协议请求字段的关联
- 客户端身份信息与协议调试记录的一致性核对
- 不同客户端版本之间的设备标识差异

相关数据可用于授权测试环境下的客户端分析，辅助核对协议请求所使用的账号身份与客户端侧身份信息之间的对应关系。

**说明：** 设备标识与账号身份并非完全等价。设备标识一致不代表服务端一定将请求识别为同一账号，实际结果还可能受到认证凭证、会话状态、客户端版本及服务端校验策略等因素影响。

---

## 版本说明

### 8.7.9

当前最新版本，versionCode `26101910`。

8.7.9 启用了网易易盾加固：APK 内只剩 80 KB 壳 dex，业务代码在加密的
`assets/nedata.db` 里，静态取不出来。资源表与 8.7.7 完全一致（9 类作弊标记、
人脸校验规则、组件表均未变），因此本目录按 8.7.7 的业务面维护，
只在 `crypto.py` 更新了 `APP_VERSION` / `BUILD_VERSION`，标注为待真机验证。

### 8.7.7

当前维护版本。

主要用于跟进闪动校园 8.7.7 客户端的协议变化。

### 8.7.4

闪动校园 8.7.4 协议实现。

### 8.7.2

闪动校园 8.7.2 协议实现。

---

## 使用说明

**完整文档见 [docs/USAGE.md](docs/USAGE.md)**，包含环境准备、登录流程、三种传输模式、全部查询工具、跑步完整链路、参数逐一说明、判定探针与常见问题。

### 快速开始

```bash
cd src/8.7.7                                  # 进对应客户端版本目录
pip install -r requirements.txt               # 装依赖（pycryptodome + requests）

python login_cli.py -u 手机号 -p 密码          # 登录，只需一次
python policy_cli.py                          # 看本校规则、围栏坐标、人脸认证状态

python run_cli.py --dry-run -d 2000 -p 360 --dump traj.json   # 试算，不联网
python run_cli.py -d 2000 -p 360 --verify                     # 正式跑
```

> **坐标不用自己填** —— 不指定 `--lat / --lng` 时，程序会自动从学校围栏接口取打卡点，并用第一个点作为起跑位置。需要手动指定时见 [USAGE.md](docs/USAGE.md#坐标不用自己找)。

各版本协议实现相互独立，**请勿混合使用**；`session.json` 不跨版本共享。

---

## 轨迹模拟

`running_protocol.py` 负责把一次跑步展开成完整数据包（`pois` / `stepList` / `paceList` / `strideList` / `targetPoints`），
并保证距离、步数、时长三组交叉校验关系自洽。路线支持跑道、环线、街区与指定打卡点四种形态。

```bash
# 离线生成，落盘检查
python run_cli.py --dry-run -d 2000 -p 360 --lat 36.6512 --lng 117.1201 --dump traj.json

# 完整链路：起跑 → 分块上传 → 结算
python run_cli.py -d 2000 -p 360 --lat 36.6512 --lng 117.1201

# 自洽性 + 统计分布自检
python traj_selftest.py

# 判定探针：只读规则 / 回查跑单 / 最小里程实跑
python risk_probe.py --config-only
python risk_probe.py --detail <runRecordCode>
python risk_probe.py --lat 36.65 --lng 117.12 -d 800 -p 420 --clean
```

常用参数：`--shape track|loop|grid|waypoints`、`--fence CODE,LAT,LNG`、`--seed`（固定可复现）、
`--chunk-seconds` / `--chunk-delay`（上传节奏）。判定口径见根目录 `docs/detection-test-plan.md`。


---

# License

本项目未附加开源许可证，默认保留所有权利，仅供个人技术研究与学习交流使用。
