# 闪动校园 8.7.9 静态逆向报告

> 样本：`shandong_8.7.9.apk`（应用宝官方分发）
> 采集：2026-10-22
> 工具：REA 6.7.0（jadx provider）+ androguard 4.1.4 + 自写 AXML/ARSC 解析

---

## 一、样本信息

| 项 | 值 |
|---|---|
| 包名 | `com.huachenjie.shandong_school` |
| versionName | 8.7.9 |
| **versionCode** | **26101910** |
| APK 大小 | 296,749,763 B（283.0 MB） |
| MD5 | `520303F6F25BCBC3D5CCEB3DD9DF0A34` |
| 解压后 | 410.0 MB，9403 个条目 |
| 签名 | `META-INF/RUNNING.RSA` |

对照样本：8.7.7（`shandong_8.7.7.apk`，303,780,056 B，MD5 `35DAE2026429591581207A550D97B57D`）。

---

## 二、最大的变化：8.7.9 开了加固

| 项 | 8.7.7 | 8.7.9 |
|---|---|---|
| dex 数量 | 23 个，合计 **136.8 MB** | **1 个，80 KB** |
| 业务代码 | APK 内可直接反编译 | `assets/nedata.db`，52 MB，熵 8.0，加密 |
| 壳类 | 无 | `com.netease.nis.wrapper.*`（MyApplication / Entry / MyJni / plugin / InstrumentationProxy） |
| 相关 so | `libnesec.so` 1.07 MB、`liby.so` 0.46 MB | `libnesec.so` 1.29 MB、`liby.so` 1.20 MB |
| 其他 | — | `assets/nedig.properties`（加密配置） |

**结论：8.7.9 使用网易易盾（NetEase NIS）加固。** 静态反编译只能拿到 80 KB 的壳，
真实 dex 在运行时由 `libnesec.so` 解密并 `DexClassLoader` 加载。

REA（jadx provider）实测：`inspect-android-package` 对 8.7.9 返回
`class_count = 31`、`resource_count = 9403`、`versionCode = 26101910` —— 31 个类
就是壳本身，业务类一个都取不到。同样命令对 8.7.7 可正常展开到数万个类。

已核验：`assets/nedata.db` 头 16 字节为随机密文，不含 `dex\n035`、`PK\x03\x04`、`SQLite format 3` 任何已知容器魔数。

---

## 三、业务面比对（未变）

### 3.1 资源名与文案

| 检查项 | 8.7.7 | 8.7.9 | 差异 |
|---|---|---|---|
| 作弊类型资源 | 9 条 | 9 条 | **0** |
| 人脸校验规则 | 5 条 | 5 条 | **0** |
| 资源名总数 | 18197 | 18181 | -16（全部为搭子/社区类） |
| 中文字符串交集 | — | — | 仅 3 条社区文案被删 |

九类作弊标记（两版完全一致，取自资源表）：

```
设备连接Adb        模拟器运行         Magisk+(%s)
应用被多开         使用网络代理       第三方ROM+(%s)
设备已root         签名被篡改        使用fakeLocation
```

人脸校验规则：

```
按学校要求需校验 %1$d 次，并保证 %2$d 次校验成功，人脸校验结果才算通过。
人脸校验结果不通过，会导致本次跑步成绩无效。
```

### 3.2 组件与 so

- Manifest 组件表基本一致，仅 `run.page.sun_run.*` 改名为 `run.page.sunshine_run.*`
- `libapp.so`（Flutter AOT）与 8.7.7 **哈希相同**
- `libNetHTProtect.so` 与 8.7.7 **哈希相同**
- 新增 7 个 layout：`fragment_face_check_camera` / `fragment_face_local_check_camera` /
  `fragment_face_upload_camera` / `run_dialog_face_check_tip` 等（人脸校验 UI 细化）

---

## 四、协议面（8.7.7 基线，8.7.9 沿用）

从 8.7.7 的 23 个 dex 里完整取出的跑步域接口共 28 个：

```
run-front/run/checkSunRunConfig      run-front/run/getPreRunCheckIn
run-front/run/plan/selectList        run-front/run/runSettings
run-front/run/startSunRun_v2         run-front/run/finishSunRun_v2
run-front/run/startFreeRun           run-front/run/finishFreeRun
run-front/run/uploadRunRecord        run-front/run/uploadStepsRecord
run-front/run/uploadPaceRecord       run-front/run/uploadPassPoint
run-front/run/uploadStrideRecord     run-front/run/queryUnFinishRun
run-front/run/resume                 run-front/run/runRecordDetail
run-front/run/pageSunRunRecord       run-front/run/pageFreeRunRecordList
run-front/run/queryRunRecordCount    run-front/run/querySchoolRunSummary
run-front/run/appealRunRecord        run-front/run/pageAppealList
run-front/run/abnormalFinishSunRun   run-front/run/abnormalFinishActivityRun
run-front/run/free/detail            run-front/run/nfc/verification-status
run-front/run/stadiumDeductionList   run-front/run/sunRunMedal/waitReceiveMedal
```

人脸相关只有四个**录入/查询**接口：

```
run-front/face/bindUserFace     run-front/face/updateUserFace
run-front/face/getUserFaceImg   run-front/face/closeUserFace
```

**没有独立的「人脸抽查上传」接口** —— 抽查结果只能走 `finishSunRun_v2` 的
`faceCheckRecordList` 字段。这一点对判定测试很关键：空数组就是空，服务端能直接看出来。

---

## 五、未能静态核验的部分

| 项 | 原因 | 验证方式 |
|---|---|---|
| 真实 dex / 接口表是否有微调 | 易盾加密容器 | 真机 dump（见下） |
| 请求签名是否换 key | 同上 | 抓一次登录/起跑请求比对 |
| `faceCheckRecordList` 新版结构 | 同上 | 运行时 hook |

### 运行时取真代码的两条路

1. **整包 dump（推荐）**：真机 root + LSPosed/Frida，在 `attachBaseContext` 之后、
   `Application.onCreate` 之前 dump 所有 `classes*.dex`。仓库里已有 8.7.7 的 Hook 模块可作模板。
2. **抓包比对（成本最低）**：真机装 8.7.9 + 系统证书，跑一次 `startSunRun_v2`，
   看 `appVersion` / `buildVersion` / `sign` 是否与 8.7.7 的算法一致。
   只需要一条请求就能确认版本常量与签名是否变。

---

## 六、对协议实现的影响

`8.7.9/` 目录已按 8.7.7 业务面建立，只改了：

```python
APP_VERSION  = "8.7.9"       # crypto.py
BUILD_VERSION = "26101910"   # = versionCode
```

其余（签名算法、字段表、上传链路、轨迹生成）沿用 8.7.7，标注为**待真机验证**。

判定测试流程见根目录 `检测判定测试方案.md`，探针脚本 `8.7.9/risk_probe.py`。

---

## 七、工具链与复现命令

### 7.1 REA 接入

仓库：<https://github.com/morluto/rea>（TypeScript，MCP + CLI，MIT）。
默认从 npmjs 安装，本机直连会 `ECONNRESET`，换 npmmirror 可用：

```bat
set JAVA_HOME=D:\downloads\com.huachenjie.shandong_school-main\.tools\jdk-21.0.12.1+1
set REA_JADX_MCP_JAR=D:\downloads\com.huachenjie.shandong_school-main\.tools\jadx-headless-mcp-0.7.1-all.jar
npx -y --registry=https://registry.npmmirror.com rea-agents@6.7.0 doctor
```

`doctor` 结果：node / host / jadx 三项 healthy，ghidra、hopper、IDA 未配置
（APK 分析只用到 jadx，不影响）。

### 7.2 常用命令

```bat
:: 包信息（版本、权限、类数量）
npx -y rea-agents@6.7.0 inspect-android-package <apk> --format json

:: 类成员清单
npx -y rea-agents@6.7.0 inspect-android-class <apk> com.huachenjie.common.bean.CheatType --format json

:: 方法反编译
npx -y rea-agents@6.7.0 inspect-android-method <apk> --procedure "com.huachenjie.common.bean.CheatType.<clinit>" --format json

:: 类名检索
npx -y rea-agents@6.7.0 search-android-classes <apk> Cheat --format json
npx -y rea-agents@6.7.0 search-android-classes <apk> FaceCheck --format json
```

注意：`decompile` / `analyze` 走的是 Ghidra/Hopper/IDA 原生分析器，**不接受 APK**，
要用上面三个 `inspect-android-*` / `search-android-classes`。

### 7.3 有真机时的取数路径

```bat
:: 列出已装包
npx -y rea-agents@6.7.0 list-adb-packages
:: 拉取完整 APK 集合（带 SHA-256）
npx -y rea-agents@6.7.0 pull-adb-package com.huachenjie.shandong_school <输出目录>
```

拿到真机后：
1. `pull-adb-package` 确认线上分发的确实是加固包（比对 MD5 与本文第一节）
2. 抓一次 `startSunRun_v2` 请求，确认 `appVersion=8.7.9`、`buildVersion=26101910`、`sign` 算法未变
3. 要拿到新 dex，需要在 root 设备上跑 Frida/LSPosed dump —— 这是 8.7.7 那 141 个 dex 的来历
