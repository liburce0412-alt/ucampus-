# 设备、显示屏与音频

以下均为显式准备步骤。保留当前用户设置、其他无障碍服务与已有设备配对。一个设备只使用一个控制执行器。

## USB 后转无线

先在手机解锁并授权 USB，用现有 ADB 枚举实际序列号。不要复制历史热点 IP。

```powershell
& $env:UCAMPUS_ADB devices -l
& $env:UCAMPUS_ADB -s '<USB序列号>' tcpip 5555
& $env:UCAMPUS_ADB connect '<当前热点IP>:5555'
& $env:UCAMPUS_ADB -s '<当前热点IP>:5555' shell getprop ro.product.model
$env:UCAMPUS_SERIAL = '<核对后的无线IP:端口>'
```

此方式仅用于可信的本地网络；完成后按自己的需求保留或关闭无线调试。无需修改电脑防火墙或把网络类型改成专用。Python 运行时每个进程只验证并固定一次设备，断线不会切到另一部手机。

## 无障碍桥

构建 `bridge/build.ps1` 后，仅在授权设备上安装自己构建的 APK，并在系统设置中启用 `U校园控制测试` 的无障碍服务。已有安装保留签名；不要卸载重装来处理签名/版本问题。

```powershell
& $env:UCAMPUS_ADB -s $env:UCAMPUS_SERIAL install -r bridge/ucampus-bridge-display-test.apk
& $env:UCAMPUS_ADB forward --list
& $env:UCAMPUS_ADB -s $env:UCAMPUS_SERIAL forward --no-rebind tcp:8794 localabstract:ucampus_control_v1
python bridge_client.py --port 8794 displays
```

服务仅观察 `cn.unipus.cloud`，客户端只连回环地址。端口冲突时选一个未占用的端口并传 `--port`。辅助包没有网络、录音、前台服务权限；临时音频策略由另一个已授权 shell 进程负责。

厂商冻结进程可能导致服务不可用；先检查实际进程状态。有需要且获准时只调整这个辅助程序的后台策略，并记录原值，任务结束恢复。意外退出的原因没有证据时不要直接归因于省电策略。

## 副屏

使用自行取得的 scrcpy 5.0.1，在当前任务的安全边界开启独立显示：

```powershell
& '<scrcpy.exe>' -s $env:UCAMPUS_SERIAL --new-display=1440x3200 --no-vd-destroy-content --display-ime-policy=local --start-app=cn.unipus.cloud --no-audio
python bridge_client.py --port 8794 displays
& $env:UCAMPUS_ADB -s $env:UCAMPUS_SERIAL shell dumpsys SurfaceFlinger --display-id
```

保留 scrcpy 窗口即可在电脑查看进度。控制使用逻辑 display ID，原生截图使用 SurfaceFlinger 64 位 ID；两者通常不同。`workflow_session.py --display-id N --capture-display-id ID` 的短词路径只接受唯一的 scrcpy 虚拟屏映射，多个副屏或过期 ID 会停止。

想在手机前台看：先创建计划的 `pause.stop`，等进程退出并确认音频已释放，再打开 U校园。它会迁移同一个任务，不能在手机操作时让执行器继续点击旧副屏。移回后重新枚举屏幕和核对题号，再执行剩余步骤。

退出副屏时先让其回到 HOME，再关闭 scrcpy；否则可能将任务带回主屏。可显式使用 `adb -s <设备> shell input -d <逻辑副屏ID> keyevent HOME`。不要为了验证退出而抢占用户主屏。

## UID 数字音频

本探针依赖当前系统已经授予 shell 的 `MODIFY_AUDIO_ROUTING`；不自动授权、不 root、不改证书。系统不支持时停止，不能自动改为外放收音。

```powershell
& $env:UCAMPUS_ADB -s $env:UCAMPUS_SERIAL shell pm list packages -U cn.unipus.cloud
& $env:UCAMPUS_ADB -s $env:UCAMPUS_SERIAL push audio-injection/ucampus-audio-probe.jar /data/local/tmp/ucampus-audio-probe.jar
& $env:UCAMPUS_ADB -s $env:UCAMPUS_SERIAL push '<官方 scrcpy-server 文件>' /data/local/tmp/ucampus-audio-runtime.jar
& $env:UCAMPUS_ADB -s $env:UCAMPUS_SERIAL shell 'CLASSPATH=/data/local/tmp/ucampus-audio-probe.jar:/data/local/tmp/ucampus-audio-runtime.jar app_process / local.ucampus.audio.InjectionProbe check <核对的UID>'
```

最后一行替换 UID 后才运行。探针再次检查 UID 属于当前用户且只对应 U校园，拒绝目标已在录音时改路由。`check` 短暂注册并释放策略，不开始训练。正常退出必须看到 `released` 且 `policy_status=1`。

首次用可重播短词确认实际内容，之后才播放长听力。直送仅支持 PCM16、单/双声道、最长 20 秒，内部转为 16 kHz 单声道；单次播放不能自动重放。采音上限 900 秒，带设备端超时保护。

```powershell
python target_audio_capture.py --uid '<UID>' --seconds 240 --output work/capture-001
# 在另一个终端正常停止：
New-Item -ItemType File work/capture-001/capture.stop
```

普通系统录音备选需核对媒体音量、音频焦点和通话模式。会议通话模式下增长的 WAV 仍可能是静音；文件大小不证明课程内容。UID 路径不调整用户音量，禁止因为历史记录写过 0 就在收尾时将正在播放的音乐静音。

## 收尾

连续训练使用[固定版本常驻入口](WORKER.md)。同一音频代码支持已固定的 USB 与无线设备；切换传输后重新核对设备与转发，不能混用旧地址。自有整周副屏不要沿用短探针的 `--time-limit`；已有时限必须向 worker 提供真实到期时间，阶段结束后再调整。无时限不意味着无需收尾。

等待录音/采集正常结束、检查释放结果，再移除本次拥有的临时音频文件与 ADB 转发。只关闭本次启用的辅助服务，恢复其原后台策略，保留其他服务。现有版本 6 与个人签名不要被新的源码构建自动替换；回退需要自己保留的原始包和相同签名，`make_rollback.py` 仅针对既有 3→7 回退路径，不是通用修复命令。
