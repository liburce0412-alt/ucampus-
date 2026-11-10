# U校园本地训练助手

Windows + ADB 的本地控制、音频采集和流程执行工具。目标是让已审阅的训练步骤执行得更快，并尽量让手机主屏继续可用。

本项目不调用模型、不提供题库或账号，也不自动发现并提交整周作业。助手或使用者负责读取题目、准备回答和批准路线；程序执行已确定的步骤并核对应用状态。

## 当前能力

| 目标 | 已有实现 | 验证范围 |
|---|---|---|
| 减少模型往返 | 持久无障碍连接、状态差异、语义点击、整阶段计划执行 | 旧组件有真机证据；新计划执行器通过离线测试，待完整真机验收 |
| 不占手机主屏 | 无线 ADB、scrcpy 副屏、显式显示屏控制 | 已在一台 Android 17 / 1440×3200 设备完成剩余四项听力 |
| 无需外放收音 | 电脑 PCM → U校园录音；只采 U校园 UID 的播放 | 短词直送、长听力采音、短时音乐共存已有验证 |
| 避免误操作 | 页面锚点、过期快照拒绝、选择后回读、未知结果不重试 | 新计划执行器含断线、动画变化、暂停与清理测试 |
| 可量化 | 本地步骤耗时/调用计数、独立 token 元数据审计 | 尚无同条件整周耗时或总 token 降幅结论 |

已测无线同页读取中位数 **2265 → 8.09 ms**，不代表整周快 280 倍。完整状态数据 **33347 → 2895 字节**，也不代表计费 token 减少 91%。[测量口径与限制](docs/BENCHMARKS.md)。

一次真实整周训练的执行环境、成绩、失败模式与实测用量见[真实整周执行记录](docs/REAL-RUN-2026-11-08.md)；为什么逐步点击比整阶段计划贵一个量级，见[会话用量与往返口径](docs/TOKEN-NOTES.md)。手势与坐标见[手势手册](docs/GESTURES.md)。

## 环境

- Windows、PowerShell 7、Python 3.11+（本地验证使用 3.13）。
- 现有 Android SDK Platform Tools；构建辅助程序需要 JDK 21、Android SDK Platform 36、Build Tools 36.0.0。
- 目标应用包名 `cn.unipus.cloud`。历史实测版本 2.8.8；其他版本先校准页面和音频。
- 副屏与数字音频复用用户自行取得的 **scrcpy 5.0.1** 运行时。其他版本未验证其内部类兼容性。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:UCAMPUS_ADB = '<已有 adb.exe 的完整路径>'
$env:UCAMPUS_SERIAL = '<本次已授权设备序列号或 IP:端口>'
$env:UCAMPUS_WORK = Join-Path $PWD 'work\current'
python -X utf8 skill/scripts/doctor.py
```

请使用已激活的虚拟环境或显式使用 `.venv/Scripts/python.exe`。本地语音转写、视频流解码才需要 `requirements-optional.txt`；不会自动下载模型。默认使用仓库内 `skill/scripts`，也可用 `--skill` 指定现有技能，保留原环境。

## 准备设备

按 [设备与音频设置](docs/SETUP.md) 完成 USB 授权、无线连接、辅助服务和音频路径检查。准备是显式操作；启动 Python 客户端不会安装 APK、启用无障碍、修改音量或创建防火墙规则。

辅助程序源码在 `bridge/`，不含个人签名密钥。构建只生成文件，不安装：

```powershell
pwsh -File bridge/build.ps1 -Jdk '<已有 JDK 目录>'
pwsh -File audio-injection/build.ps1 -Jdk '<已有 JDK 目录>'
```

已安装旧辅助包时，继续使用原签名与合适的版本号；不要用新密钥覆盖或卸载现有服务来绕过签名冲突。`bridge/build.ps1 -KeyStore` 可指向原来的本地测试密钥。

## 一次执行已审阅的阶段

先读一次实时控件，准备带页面/题目锚点及后置条件的 JSON 计划，再执行整个阶段。

```powershell
python bridge_client.py --port 8794 --display-id 0 observe
python workflow_session.py examples/navigation.json --validate
python workflow_session.py '<审阅后的计划.json>' --output work/run-001 --port 8794 --display-id 0
```

示例中的标题和控件文字是占位内容，必须按实际页面修改。`--validate` 不连接手机，但会校验引用的本地 WAV。完整[计划格式](docs/WORKFLOWS.md)包含：

- `click`：语义点击并等待新后置状态，可连续导航/提交。
- `select`：核对题目锚点、选择答案、回读 `checked/selected`；已选中的答案不重复点击。
- `word`：用已审阅、最长 20 秒的 WAV 完成校准后的 Pre 短词录音，再回读应用状态。
- `capture`：先确认专属采音就绪，再点击播放，等待结束条件并正常释放音频策略。
- `wait`：等待应用状态，无固定长睡眠。

音频步骤需要 `--uid <本次核对的应用UID>`。副屏短词还需要 `--capture-display-id <SurfaceFlinger 64位ID>`；不猜测主副屏映射。计划不支持任意 shell 命令、盲点坐标列表或未知结果自动重发。

程序只输出一次简短摘要，完整结果写入 `work/run-001/checkpoint.json`。中断后先读取实际题号/录音状态，重新编写剩余步骤；输出目录不可重用，防止盲目重放。需要让出进度查看时，在另一个终端：

```powershell
New-Item -ItemType File work/run-001/pause.stop
```

等进程退出并检查断点后再切屏。短词录音会先完成有界清理，暂停不等于立即中断音频。手机打开 U校园会移动同一个任务窗口，不能当作互不影响的第二份界面。

2026-11-10 修复了页面跳转时短暂 `no_active_window` 导致计划提前退出的问题：后置状态在原超时范围内继续只读等待。只有设备明确在动作派发前返回 `ui_changed/node_changed`，才重新核对锚点后有限重试；锁屏、外来窗口和未知点击结果仍停止。[本次耗时复盘](docs/BENCHMARKS.md#2026-11-10-实际耗时回归)显示当前整条流程仍比早期执行慢，不能把控件读取加速当作任务提速。

## MCP 与已有执行入口

`bridge_client.py --port 8794 --display-id 0 serve` 是 stdio MCP，提供 `ucampus_observe`、`ucampus_click`、`ucampus_batch`，复用同一连接。用 [配置示例](codex-mcp.example.toml) 手动指定路径；仓库不会修改全局配置。

长时间录音/播放计划通过 `workflow_session.py` 运行，避免受 MCP 单次调用超时限制。`ucampus_readonly.py --ui-dir work/current serve` 只读取已保存 XML 和静态研究摘要。

其他保留入口：

| 文件 | 用途 |
|---|---|
| `training_session.py` | 范例整组跟读，复用原校准逻辑 |
| `dialogue_session.py` | 已审阅 AI 回答列表，严格核对话轮；完整新版流程待真机验证 |
| `audio_injection_trial.py` | 单次有界数字音频注入 |
| `target_audio_capture.py` | U校园 UID 专属采音与正常停止文件 |
| `screen_frames.py` | 可选视频帧复用、严格副屏映射 |
| `static_audit.py` / `dynamic/` | 用户自备 APK 静态分析与授权动态观察 |
| `usage_audit.py` | 统计指定本地日志时间段中的 token 元数据 |

## 测试与量化

```powershell
python -X utf8 -m unittest discover -p 'test_*.py' -q
python -X utf8 -m unittest discover -s dynamic -p 'test_*.py' -q
node --test dynamic/test_cdp_observe.cjs dynamic/test_webview_probe.cjs
```

这些测试不操作手机。CI 执行同一套离线检查。[验证记录](docs/VALIDATION.md)区分历史真机结果、新代码离线验证和待验收项目。

```powershell
python usage_audit.py '<本地会话.jsonl>' --start '2026-11-04T12:00:00+08:00' --end '2026-11-04T13:00:00+08:00'
```

输入 token 中已包含缓存；审计另外列出未缓存输入。推理 token 不重复加入输出。它不读取账号凭据、不输出聊天正文、不估算价格，也不能将不同题型/调试负载直接比较成节省比例。

`usage_audit.py` 读的是 Codex 风格的 `event_msg`/`token_count` 记录。DSH 原生日志（schema v4）
把用量放在每条 `assistant/message` 的 `data.usage` 里，直接审计会得到 `usage_updates: 0`
而不报错。对这类日志用同字段、同校验的 `usage_audit_dsh.py`：

```powershell
python usage_audit_dsh.py '<本地会话.jsonl>' --start '2026-11-01T00:00:00+08:00' --end '2026-11-12T00:00:00+08:00'
```

两者都要求显式时区、都拒绝计数器重置，都不输出对话正文。

## 边界与来源

不上传 APK、反编译源码、课程答案、账号令牌、个人录音或原始会话日志。`work/`、音视频、密钥和构建产物默认忽略；分享前仍需检查 Git 暂存区。

本仓库仅发布本地工具源码、脱敏结论与合成测试夹具。参考了 [Mika](https://github.com/wangzan101/Mika) 的连接/界面复用和批处理思路，运行时复用 [scrcpy](https://github.com/Genymobile/scrcpy)。[第三方说明](THIRD_PARTY.md)；[APK 研究结论](docs/APK-ANALYSIS.md)。
