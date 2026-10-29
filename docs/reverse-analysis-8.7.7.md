# 闪动校园 8.7.7 逆向分析报告

> 生成时间：2026-10-21
> 工具链：REA 6.7.0 + jadx-headless-mcp 0.7.1 + Temurin JDK 21
> 目标：`Inverx/com.huachenjie.shandong_school`（MIT）

---

## 一、数据来源与修正

### 问题：本地 FridaDump 是空的

原目录 `闪动校园8.7.7-FridaDump/` 下 143 个 dex **全部是 Git LFS 指针**（每个 128–132 字节）：

```
version https://git-lfs.github.com/spec/v1
oid sha256:7d9b8b50e03f39bfb55c3f61d622bf6e9219d1745053eb7090fe0e20defca953
size 4594236
```

`.gitattributes` 已声明 `闪动校园8.7.7-FridaDump/** filter=lfs`，zip 下载不携带 LFS 对象。

### 解决

```
git clone --depth 1 https://github.com/Inverx/com.huachenjie.shandong_school.git .upstream
git lfs pull --include="闪动校园8.7.7-FridaDump/**"     # 168 MB
git lfs pull --include="设备采集-*/*_8.7.7.apk"         # 290 MB
```

### 实际取得

| 产物 | 数量 | 体积 | 校验 |
|---|---|---|---|
| dex 文件 | 143 个 | 161.2 MB | 全部有效，魔数 `dex\n035/037/038/039` |
| 总类数 | 156,357 | — | 解析 class_defs 得出 |
| 目标 APK | 1 个 | 290 MB | `PK` 头正常 |

LFS 全量清单 287 个对象 / 1098.7 MB，其中 608 MB 是两个重复的 304 MB APK，322.4 MB 是 pro 2.0.9 的 dump，均未拉取。

---

## 二、目标 App 架构画像

### 2.1 加固与安全组件

| 组件 | 证据 | 说明 |
|---|---|---|
| 网易易盾加固 | `libnesec.so` (1.02 MB)、`libnesec-x86.so` | dex 壳，直接影响静态分析的完整性 |
| 360 加固 SDK | `libjiagu_sdk_OSETSDKProtected.so` | 与易盾共存 |
| 阿里聚安全 | `libsecsdk.so`、`libsgcore.so` | 运行时安全组件 |
| 阿里无线安全 | `com.alibaba.wireless.security.open.*` | 动态/静态数据加密接口 |
| 字符串混淆 | `com.zj.zk.appsdkdex.crypt.WMStringFogImpl` | 局部字符串雾化 |

### 2.2 架构特点

**Flutter 混合架构**（`rea identify-flutter-build` 实测）：

```
flutter_detected: true
snapshot_hash:    d20a1be77c3d3c41b2a5accaee1ce549   (arm64 / armeabi-v7a 一致)
libapp.so:        6,030,256 B  sha256 f96590d5…
libflutter.so:   10,601,880 B  build_id 3b363e6e…
Dart 工具链:      clang 17.0.2 (Android 10552028)
```

即核心业务逻辑有一部分在 **Dart AOT 编译产物里**，不在 dex 里。这解释了为什么纯 Java 层搜不到签名算法。

### 2.3 其他关键 native 库

- `libInspireFace.so`（5.08 MB）— 人脸识别 SDK
- `libAMapSDK_MAP_v9_5_0.so`（12.75 MB）+ `libBaiduMapSDK_base`（0.49 MB）— 双地图 SDK
- `libqmcheat.so`、`libNetHTProtect.so` — 风控/作弊检测相关
- `libflutter.so`、`libapp.so`、`libtensorflowlite_jni.so` — Flutter + 端侧推理

### 2.4 包结构（15.6 万类 TOP）

| 类数 | 包 |
|---|---|
| 20,576 | `com.zj`（广告聚合 SDK） |
| 10,252 | `com.ltmb`（广告 SDK） |
| 10,168 | `com.huachenjie`（**主业务**） |
| 10,029 | `com.kc` |
| 9,034 | `com.google` |
| 8,349 | `com.byazt`（字节广告） |
| 6,079 | `com.kwad`（快手广告） |

`com.huachenjie` 下**不存在**类名含 `encrypt`/`decrypt`/`cipher` 的类 —— 业务加密不在 Java 层。

---

## 三、密钥溯源验证（核心结论）

拿 `8.7.7/crypto.py` 里声明的 5 个密钥，在 143 个 dex + 93 个 arm64 so 中做完整二进制搜索（ASCII + UTF-16LE + 片段级）。

| 密钥 | 值 | 搜索结果 | 判定 |
|---|---|---|---|
| AES IV | `01234ABCDEF56789` | **命中 `libapp.so`**（Dart 字符串池，@0x39b21） | ✅ 真实，位于 Flutter 层 |
| AES_FIELD_KEY 前半 | `F44B0282BEA83557` | **命中 3 个 dex**（classes7/12/16） | ⚠️ 字符串存在，语义未确认 |
| SLIDER_KEY | `Ukp3hmSe7BmMcgbE` | **命中 classes11.dex**（@0x663446） | ✅ 真实 |
| API 域名 | `api.huachenjie.com` | **命中 classes12.dex**（3 处） | ✅ 真实 |
| AES_SIGN_KEY 前半 | `RHXL092CDOYTQJVP` | **全部未命中**，含片段 `CDOYT`/`QJVP` | ❌ 8.7.7 中无明文 |
| runImgRecord salt | `2V8BQ8MYXWU10Y9Z` | **全部未命中**，含片段 `2V8BQ`/`MYXWU`/`0Y9Z` | ❌ 8.7.7 中无明文 |

### 判定依据

`RHXL` 三个字符在 4 个 so 中有命中，但全部是广告 SDK（`libPglbizssdk_ml.so`、`libgrowease.so`、`libhigh_available_android.so`）的无关字节序列，与业务无关。

连续 4 字符 `CDOYT`、`2V8BQ` 在 236 个可执行产物中零命中 —— 已排除「字符串被拆成多段拼接」的可能（拆得再碎也至少会留下 3-4 字符的连续片段）。

### 结论

`AES_SIGN_KEY` 与 `runImgRecord salt` **不是 8.7.7 版本的静态常量**。可能来源：

1. 运行时由服务端下发或 native 动态生成
2. 通过 Frida hook 抓取的运行时值（dump 里看不到）
3. 作者从其他版本（8.7.2/8.7.4）或动态调试中取得，未随版本更新核对

**实践含义**：依赖这两个值构造请求时，若签名校验失败，优先怀疑取值来源而非算法实现。

---

## 四、Hook 模块分析（`模块/Hook闪动-v8.7.7-debug.apk`）

5.4 MB 真实 APK，2799 个类，包名 `caom.inverx.huachenjie`（Xposed/LSPosed 模块），`caom` 包下 138 个类，15 个顶层类：

| 类 | 职责（由方法名与方法体确认） |
|---|---|
| `SslBypassHook` | 9 个 hook 点：`hookFrameworkTrustManagers` / `hookNetworkSecurityPolicy` / `hookSslContext` / `hookHttpsURLConnection` / `hookWebView` / `tryHookOkHttp` / `hookApplicationLifecycle` / `hookAntiDesktopTransitions` / `isHomeIntent` |
| `FaceHook` | `bitmapToNV21` / `buildTrackingInfo` / `buildZeroFrame` / `pickPhoto` / `decodePhoto` / `hookFaceTool` / `hookUserFaceBean` — 摄像头帧替换 |
| `FakeLocationHook` | `hookBDLocation` / `hookSystemLocation` / `hookHCJAddressInfo` / `hookCheatDetector` / `hookCheatingDetectionComponent` |
| `AntiEmulatorHook` / `AntiRootHook` | 反检测绕过，各 10+ 内部类 |
| `MainHook` | `handleLoadPackage` / `installAntiKill`（防进程查杀） |
| `KeyAuditHook` | `hookBusinessClasses(ClassLoader)` — 密钥审计 |
| `WifiHook` / `FaceCacheSync` / `HookLogger` / `ModuleConfig` / `LateBinder` | 辅助 |

### 反编译实例（REA + jadx 实测输出）

`caom.inverx.huachenjie.FakeLocationHook.hookCheatDetector`：

```java
public static void hookCheatDetector(Class<?> cls) {
    if (cheatDetectorHooked.compareAndSet(false, true)) {
        for (Method method : cls.getDeclaredMethods()) {
            if ("b".equals(method.getName())) {
                XposedBridge.hookMethod(method, new XC_MethodHook() {
                    protected void beforeHookedMethod(MethodHookParam p) {
                        if (ModuleConfig.isLocationEnabled()) {
                            p.setResult(new HashMap());   // 清空作弊特征探测结果
                        }
                    }
                });
            }
        }
        HookLogger.logOnce("loc-oj1-disabled", TAG, "底层作弊特征探测器 oj1.b 已清空拦截");
    }
}
```

这说明 App 侧存在名为 `oj1` 的作弊特征探测器，其 `b` 方法返回检测结果，Hook 侧直接以空 `HashMap` 覆盖。

---

## 五、工具链可用性记录

| 命令 | 状态 | 备注 |
|---|---|---|
| `identify-flutter-build` | ✅ | 纯 APK 解析，290 MB 正常 |
| `extract-artifact` | ✅ | 792 节点 / 846 条目产物图 |
| `inspect-android-class` | ✅ | 74,089 类，加载 32.9 s |
| `inspect-android-method` | ✅ | 完整 Java 源码输出 |
| `analyze`（对 APK） | ❌ | 只接 native provider，拒绝 archive |
| `decode-android-resources` | ❌ | 缺 apktool（`REA_APKTOOL_COMMAND`） |
| Ghidra provider（Windows） | ❌ | `Win32 87: requires an absolute executable path` |

### 必需的运行时

- JDK **必须完整版**（含 `jdk.compiler`），JRE 会报 `A JRE cannot compile REA's source bridge`
- 位置：`.tools/jdk-21.0.12.1+1`（329 MB）、`.tools/jadx-headless-mcp-0.7.1-all.jar`（41 MB）
- MCP 配置 `~/.workbuddy/mcp.json` 的 `rea` 条目已注入 `JAVA_HOME` / `REA_JADX_MCP_JAR` / `PATH`

---

## 六、遗留与建议

1. **pro 2.0.9 的 322 MB dump 未拉取** —— 如需要 Flutter/Dart 层逆向可再拉
2. **Dart 层签名逻辑未还原** —— `libapp.so` 的 snapshot（`d20a1be7…`）需要 Blutter/reFlutter 类工具处理，REA 与 jadx 都不覆盖 Dart AOT
3. **`an analyze` 对 .so 可用但需 Ghidra** —— Windows 上暂不可用，建议在 Linux/macOS 或 WSL 里做
4. **`AES_SIGN_KEY` 需动态验证** —— 建议在真机 Frida hook 时打印实际参与 AES 的 key bytes，与 `crypto.py` 比对

---

## 七、说明

本报告只做静态事实核验（文件结构、常量存在性、模块职责）。轨迹生成与数据包组装由 `running_protocol.py` 承担，生成结果可用 `traj_selftest.py` 验证自洽性与统计分布。
