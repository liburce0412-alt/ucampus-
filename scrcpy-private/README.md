# 训练专用副屏

scrcpy 5.0.1 默认把虚拟屏标为 `PUBLIC | PRESENTATION`。本机主屏视频应用曾向这个副屏创建 Presentation 窗口，遮住 U校园，导致保护逻辑暂停。这里的精确补丁只移除这两个标志，保留 `TRUSTED`、独立焦点、触控和原有生命周期处理。

补丁基于官方 `v5.0.1`，提交 `a60891aea193d92e7e5c3942700eca63f9d19a5f`。仅发布补丁、构建脚本及上游 Apache-2.0 许可，不包含上游完整源码或二进制。不要对其他版本直接套用。

## 本地构建

把本目录的文件放入一个新的工作目录，较大源码和构建输出可放在 D:。复用已有 JDK 21、Android SDK Platform 36 和 Build Tools 36.0.0；不需要 Gradle。

```powershell
git clone --depth 1 --single-branch --branch v5.0.1 https://github.com/Genymobile/scrcpy.git source
if ((git -C source rev-parse HEAD) -ne 'a60891aea193d92e7e5c3942700eca63f9d19a5f') { throw 'Unexpected upstream revision' }
git -C source apply ..\private-display.patch
if ($LASTEXITCODE) { throw 'Patch did not apply' }
pwsh -File ./build-candidate.ps1 -Jdk '<已有 JDK 21 目录>'
```

脚本按上游无 Gradle 构建流程生成 AIDL、Java 8 字节码和 DEX，输出 `scrcpy-server-v5.0.1-private`。拒绝覆盖已有 build/output，失败后先检查自己的构建目录，不删除共享工具。手工构建包仅含 `classes.dex`，与上游 Gradle 包的大小不同。

本机已验证 ZIP/DEX 完整性、91 个 Java 顶层类无遗漏、版本检查接受 5.0.1 并拒绝 5.0.0，且独立干净重建得到相同文件。初始显示标志从 203 改为 200。

## 使用与迁移

保持正式 scrcpy 安装不变，仅给新建的 5.0.1 子进程指定 server：

```powershell
# 由操作者填入已核对的程序路径和本次设备、显示参数。
$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = $scrcpyExe
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.Environment['SCRCPY_SERVER_PATH'] = (Resolve-Path './scrcpy-server-v5.0.1-private').Path
foreach ($argument in $existingScrcpyArgs) { $start.ArgumentList.Add($argument) }
$process = [Diagnostics.Process]::Start($start)
```

不修改全局环境、正式 server 或手机主屏应用。新建后核对显示标志确为 PRIVATE、没有 PRESENTATION，并核对 U校园窗口、控件树与截图。

迁移已有训练时，先停执行器并确认无录音/采音，核对该 RootTask 只属于 U校园。系统支持时用 `am display move-stack <RootTask ID> <新逻辑显示ID>` 直接移到新屏。确认任务、题号及显示映射，再关闭本次拥有的旧 scrcpy 进程；双屏并存期间，执行器的唯一映射保护会拒绝坐标输入。关闭前旧屏应已没有 U校园，不能靠关闭旧屏把训练弹回主屏。

本机迁移后厂商 ActivityManager 的可见窗口缓存曾仍指向旧屏。执行器使用无障碍目标焦点与 WindowManager 实际可见表面共同判断；仍拒绝外来可见窗口。`dumpsys window displays` 的显示标题可能接在上一行末尾，解析不能假定标题在行首。不能用过期缓存或错误分段宣称主屏未受影响。

当前已验证一次同任务副屏迁移、控件树及三项完整口语训练；本次为 USB 控制，完整运行结果见[验证记录](../docs/VALIDATION.md)。该补丁减少 Presentation 自动发现入口，不保证所有厂商、所有悬浮窗都不会遮挡；原有遮挡保护继续生效。

## 来源

- [上游虚拟屏实现](https://github.com/Genymobile/scrcpy/blob/v5.0.1/server/src/main/java/com/genymobile/scrcpy/video/NewDisplayCapture.java)
- [上游无 Gradle 构建](https://github.com/Genymobile/scrcpy/blob/v5.0.1/server/build_without_gradle.sh)
- [server 环境变量入口](https://github.com/Genymobile/scrcpy/blob/v5.0.1/app/src/server.c)
- [AOSP 私有显示定义](https://github.com/aosp-mirror/platform_frameworks_base/blob/android16-release/core/java/android/hardware/display/DisplayManager.java)
- [AOSP 显示启动检查](https://github.com/aosp-mirror/platform_frameworks_base/blob/android16-release/services/core/java/com/android/server/wm/ActivityTaskSupervisor.java)
