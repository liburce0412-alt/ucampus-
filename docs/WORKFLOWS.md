# 已审阅计划

计划是 `{"version":1,"steps":[...]}`，1–64 步。每步 `before` 必须提供 1–8 个精确页面/题目锚点；选择器可以组合 `text`、`id`、`description`。同名匹配多于一处时停止，不猜最近的按钮。

```json
{
  "version": 1,
  "steps": [
    {"action":"select","before":[{"text":"已核对的题目全文"}],"target":{"text":"已审阅的选项全文"}},
    {"action":"click","before":[{"text":"已核对的题目全文"}],"target":{"description":"提交"},"expect":{"text":"该题提交后的唯一状态"},"timeout":30}
  ]
}
```

`select` 必须能读到选项或其可点击父节点的 `checked/selected`。部分自绘选项没有此状态，需要人工回看或继续使用原已校准工具，不能仅凭点击成功提交。点击前重新读状态，Android 端再验证快照与节点，因此不会把滚动前的坐标直接重用。

`click` 的 `expect` 在点击前不能已经可用，防止把旧状态误当新提交成功。`before` 用于点击前校验，页面跳转后允许消失。`wait` 只等待 `expect`，`timeout` 为 1–900 秒，默认 30 秒。提交时选择能够证明进入下一阶段的状态；“已提交 等待评分”单独不足以证明录音已被保存。

## 短词直送

```json
{"version":1,"steps":[
  {"action":"word","before":[{"text":"Pre-listening"},{"text":"example"}],"word":"example","wav":"speech/example.wav","xy":[720,1800],"expect":{"text":"实际观察到的录音后状态"}}
]}
```

这里的麦克风坐标只是格式示例，必须来自本次截图。先用一条可重播短词校准，并设置 `UCAMPUS_RECORDING_SIZE=1440x3200`。WAV 相对计划文件解析；全计划所有 WAV 在第一次设备操作前检查格式、长度和非静音。程序等待真实录音状态，数字声音仅送到目标 UID；没有静默退回外放收音。

短词输出 `audio_content_verified=false`：代码完成传输不等于应用收到正确内容。首次音频路径需要回放/波形或转写证据；正常执行不为分数重录。长自由表达超过 20 秒仍未覆盖。

## 先采音再播放

```json
{"version":1,"steps":[
  {"action":"capture","before":[{"text":"While-listening"}],"target":{"description":"播放"},"expect":{"text":"播放结束后才可用的入口"},"seconds":240,"timeout":220}
]}
```

`seconds` 必须覆盖整个等待上限；给课程长度留出启动余量。新目录下 `ready.json` 确认 UID/采集模式后才点播放。到达后置状态、失败或暂停均发送正常停止请求，等待尾部 PCM 和音频策略释放。采音提前退出时停止路线，不重放仅能听一次的课程。

声音文件及事件在 `work/<run>/step-XX/`。录音长度或 RMS 只能证明有采样，内容仍需核对。用户同时听音乐时始终使用 UID 采音，保留其音量与音频焦点设置。

## 断点、交接、测量

- 一个设备同一时间只有一个执行器。不要并行运行两个计划或让另一个助手同时点屏幕。
- `checkpoint.json` 每步前写 `in_flight`，只有后置状态通过才增加 `completed`，临时文件原子替换。
- 失败退出 1，暂停退出 2；都不会自动从日志重放。即使 `completed=0`，`in_flight=1` 的操作也可能已被应用接受。
- 等暂停完成并确认录音/采集已释放，再移到主屏查看。同一任务移回副屏后重新枚举显示 ID、读取实际阶段，准备剩余步骤的新计划。
- 记录 `elapsed_seconds`、`bridge_reads`、`clicks` 和逐步耗时。它们是执行器指标；实际模型 token 使用 `usage_audit.py` 单独统计。

新执行器只接入了已明确的机械步骤，不会自动识别所有课程、生成回答或绕过平台解锁条件。完整 AI 对话仍使用独立候选入口，并有单独验收边界。
