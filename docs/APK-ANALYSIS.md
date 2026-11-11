# APK 分析可用结论

已对合法取得的安装版 2.8.8（`cn.unipus.cloud`，20880）与官网 2.8.9 参考包做过静态检查。以下仅发布结论与字段名，原包、反编译材料、数据库、登录数据和个人日志不在仓库内。

| 线索 | 可用于什么 | 当前证据边界 |
|---|---|---|
| `unipuscloud://open?page=ulistenspeak` | 直接到听说模块，减少入口导航 | 历史真机到达模块；不能直接定位某个周任务 |
| `tvStartTraining`、`tvWeekProgress` 等资源 ID | 首页语义定位 | 原生页可用；H5 还需 `description`，自绘录音仍须校准 |
| `UCAudioPlugin` / `UCPcmPlugin` | 播放状态、进度与自然结束候选 | 静态状态契约明确；没有把每个候选都当已命中接口 |
| `UCSOEPlugin.start/stop/result` | 录音开始、结束及结果观察 | 实机日志观察到 SOE 开始/结果；与控件状态配合使用 |
| `UCClioPlugin`、Chivox/SkEgn/SOE 原生导出 | 下一次动态观察的范围 | 原生符号存在不证明当前题型调用了它 |
| `UCHttpTaskPlugin.backgroundRequest` | 定位真实任务请求的候选入口 | 当周任务 HTTP 请求/响应/进度语义尚未打通 |
| 加密壳与大型发音资源 | 避免重复解壳和误把词典当题库 | 资源清单与结构分析，不包含题库提取 |

`UCAudioPlugin` 静态状态值：PREPARED=3、PLAYING=4、PROGRESS=5、PAUSE=6、AUTOCOMPLETE=8、ERROR=9。`UCPcmPlugin` 为 PLAYING=1、PAUSED=2、STOPPED=3、BUFFERING=4、FINISHED=5、ERROR=9，两套值不能混用。PCM 桥是播放路径，不是把电脑声音送进麦克风的直接接口；当前直送实现使用独立 UID 音频策略。

实机跟读曾观察到 `准备中…` → `录音中 点击提交` → `已提交 等待评分` → `重录`。提交文字可能早于保存，不能独立证明平台完成。2026-11-11 实测立即切句会丢失部分录音，因此最终版恢复重录入口作为保存门控，快速模式仍不读取数值分数。最终以实际阶段推进与周计数验收；保存异常只根据平台明确缺失状态补齐，不盲目重发。

SOE 的结果字段包括 `audioUrl`、`audioSOEUrl`、`recordLocalPath`、`duration`、`params`、`result`、`scores`、`recordId`、`userId`。只保留字段名；不读取或公开其中的身份值、带签名媒体地址、令牌或录音内容。

## 可复用工具

`static_audit.py <APK> <本地解包目录> <work/输出目录> --rabin <rabin2>`：分析清单、Cordova 注册、JS 桥、资源与原生导出。需要自行提供合法 APK、已有解包工具与 rabin2，不自动下载、解密或连接手机。输出中含从 APK 复制的资源，应留在被忽略的 `work/`。

`dynamic/log_observe.py` 仅观察已确认的目标进程，保留脱敏元数据。`cdp_observe.cjs` 仅连接显式指定的回环调试端点；需要应用确实提供可达 WebView 调试能力。`native_probe.js` 是可选授权 Frida 观察探针，不自动安装服务、root 或修改应用。

历史设备未找到可用的训练 WebView 调试套接字，也没有现成 Frida 服务。URL 字符串、公开官网入口和训练缓存目录不能证明能通过 HTTP 完成任务。后续以实际请求、响应和平台回读验证，避免猜接口和反复提交。
