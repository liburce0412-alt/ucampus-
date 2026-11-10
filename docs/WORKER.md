# 常驻执行与固定版本

`training_worker.py` 在一个进程里复用控制连接、已缓存的 base 转写模型和本轮 TTS 文件。模型只接收题意判断请求、阶段结果或异常；录音、选择和翻页留在本地执行。它不是题库，也不绕过平台状态校验。

## 准备一次运行

先用已有 Python 环境检查依赖，确认所选 USB 或无线设备、U校园 UID、桥接转发归属、副屏映射和实际录音布局。新入口使用仓库自带的运行时；不再从临时目录复制修改 USB 版本。

```powershell
python freeze_runtime.py work/runtime-run001
```

该命令只复制执行源码和技能脚本到新目录，记录文件散列，不复制音频、日志、密钥、账号或环境文件。后续改源码不会改变这个副本；运行前验证副本，执行中若依赖文件发生变化则停止。始终从这个副本启动，输出证据目录也必须全新。

```powershell
$env:UCAMPUS_RECORDING_SIZE = '1440x3200' # 仅在实际校准此布局之后设置
python -u work/runtime-run001/training_worker.py --output work/run001 --uid <本次UID> --port 8794 --display-id <逻辑ID> --capture-display-id <SurfaceFlingerID> --xy <麦克风X> <麦克风Y> --option-bounds <左> <上> <右> <下> --display-unlimited
```

尖括号是必须替换的实测值，不是可直接运行的参数。新入口先验证录音尺寸配置、UID 与目标设备、桥接转发、显示映射和遮挡；它不会自行开启服务、改分辨率、改音量或切换账号。选项区域和麦克风坐标必须来自当前页面。当前 Pre 版只适用于已验证的 1440×3200 布局及宽标题区域，其他布局先校准。

`--display-unlimited` 仅用于本次拥有且没有 `--time-limit` 的 scrcpy 进程；任务结束仍要主动关闭。已有带时限副屏用 `--display-deadline '含时区的实际到期时间'`，执行器在阶段开始和判断等待期间检查余量，不让音频步骤跨越已知到期时间。不要把短时探针的退出时限用于整周训练。

## 每行一个 JSON 命令

通过保留 stdin 的交互终端会话运行；PowerShell 管道输完即 EOF 会触发暂停，不能用它伪装常驻会话。每次只有一个命令执行；不要同时再启动第二个手机执行器。

```json
{"command":"observe"}
{"command":"pre","capture":"已验证且正在增长的UID采音WAV绝对路径","enter":true}
{"command":"plan","plan":{"version":1,"steps":[{"action":"wait","before":[{"text":"当前实际页面文字"}],"expect":{"text":"实际后置状态"},"timeout":30}]}}
{"command":"transcribe","wav":"已有音频绝对路径","start":0,"seconds":30}
{"command":"close"}
```

Pre 的 `enter=true` 仅从尚未进入的导学页开始；已经在题内时必须用 `start` 指定本次题的已核对音频秒偏移、`start_at` 指定题号，不能重新进入或重播。单阶段最多七词。音频存在足够非静默内容、结束后才转写；唯一完整候选命中可自动选词，否则请求判断。释义始终请求判断；每次回答后重新核对题目，陈旧回答不能点到下一题。

请求和响应示例：

```json
{"type":"decision","request_id":"本次生成的ID","kind":"meaning","word":"example","choices":["选项甲","选项乙","选项丙"]}
{"request_id":"原样返回本次ID","choice":"B"}
```

`plan` 使用[既有计划格式](WORKFLOWS.md)。多个已审阅选择/提交步骤可一次执行；只逐题解锁的页面仍按实际题目边界交出判断。`capture` 会复用同一转写模型，在播放期间分段转写，正常停止后保留末尾片段。`expect` 必须是实际播放后状态，不能使用只有答完全部题才出现的「进入Post-listening」。本机归零型播放器改用 `clock`，同时核对计时推进与剩余媒体时长，不能仅看归零。

首次合成短词优先 SAPI，失败时复用 `UCAMPUS_FFMPEG` 或 PATH 中已有的 FFmpeg/libflite；不下载新工具。只有有效非空 PCM16 才成为缓存，已有 WAV 不覆盖。`speech_ready` 记录实际后端和准备耗时。

新建 `work/run001/pause.stop` 或关闭 stdin 会请求暂停（退出码 2）。正在进行的有界录音/采集先清理，断点记录在相应阶段目录。任何异常都会停止进程，恢复必须重新读取实机状态并准备新命令；不会自动重播未知结果的动作。关闭 worker 不等于关闭外部持有的副屏或采音进程，执行方仍按启动时的资源归属完成收尾。

## 记录实际收益

`manifest.json` 记录运行文件版本、设备、显示 ID、初始化和首次模型加载耗时；`events.jsonl` 记录阶段起止、判断等待、短词动作、正常完成或失败。命令可加 `"purpose":"recovery"` 或 `"purpose":"debug"`；默认 `training`。阶段总时长与判断等待会重叠，不能再次相加；判断等待也不等于纯模型计算时间。

用阶段起止时间调用 `usage_audit.py`。它自动识别 Codex 累计用量和 DSH v4 独立用量，将 `input_tokens` 统一为包含缓存的输入，另外列出未缓存输入与缓存输入。没有匹配格式、缺字段、总量不一致、单文件混合格式会明确报错；同一 DSH 请求用量值相同不代表重复。非零 DSH 缓存写入语义尚无可靠样本，暂时拒绝。不同路径的重叠日志仍需调用方避免重复输入。

比较同种任务和相同阶段，单列准备、恢复与调试。设备读取次数不是模型请求数，运行步骤完成不是平台周进度。先验收一组和一篇，再报告整体耗时/token；不把跨版本、跨设备的历史失败直接写成通用限制。

2026-11-10 候选已在真实副屏同一进程完成 observe、1 步只读计划和两段 10 秒已存音频转写，正常 close。完整 Pre/While、播放中分段转写与新 clock 条件目前仅离线测试，下一轮先验收一组和一篇；不要为验证而重做已经完成的课程。
