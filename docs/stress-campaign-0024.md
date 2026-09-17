# 0024 故障注入压测：基线臂（无 D1–D7）

## 这轮测的是什么，不是什么

用户问的是"看内核补丁是否有效"。**在 0024 上现在无法回答这个问题**，原因见下方"前提核对"：
三台 worker 跑的都是**原版 amdgpu 6.14.14**，D1–D7 一行都没编译进去，更没装上。

所以这轮压测**不是 A/B 对照的实验臂，是它的对照臂**——把"没有补丁时这台机器怎么坏、坏得多快、
我们的检测链在第几秒看见"量出来。没有这组数，将来补丁装上之后测出来的任何数字都没有参照物，
只能得出"装了补丁之后跑了 2 小时没挂"这种既不能证实也不能证伪的结论。事故率是
**4 次 / 2 节点 / 月**，靠"跑一轮没事"是统计不出来的，必须靠**受控注入下的区间长度**。

这轮要产出的是四个区间，每个都以秒为单位，而不是"通过/不通过"：

| 区间 | 从 | 到 | 现有证据 |
|---|---|---|---|
| I1 触发 → 内核首条致命行 | 注入时刻 | 首条 `Trying to push to a killed entity` | 无（历史日志里看不到触发时刻） |
| I2 内核致命行 → 首条 hung task | `killed entity` | 首条 `blocked for more than` | 0004 ≈1 分钟；0024 3 分 52 秒（F12，两个样本） |
| I3 内核致命行 → Node Condition | `killed entity` | `GPUUnrecoverable=True` | 注入 `/dev/kmsg` 时实测 19–45 秒，**真实故障下未测过** |
| I4 Condition → 告警 firing | condition 翻转 | Alertmanager firing | ≈100 秒（F9，注入路径） |

**I2 是这个项目的全部价值所在**：它是这台节点还能和"健康"区分开的最后一段窗口。
两个历史样本相差近 4 倍，所以现在这个数只能说"分钟级"，说不出别的。

## 前提核对：0024 上没有 D1–D7（2026-09-17 实测）

| 检查 | 结果 |
|---|---|
| `uname -r` | `6.14.14`，三台 worker 一致（0043 的 6.16.6 是**用户态 ROCm** 版本漂移，见 A5） |
| `modinfo amdgpu` srcversion | `4B692E1E808F34CA3FC4555`，与发行版原版一致 |
| 模块参数 | 100 个，逐个比对全部为上游参数，无补丁新增项 |
| D1–D7 sysfs 计数器 | **0 个**。`d4_watchdog_fired_total` / `d1_remediate_fences_total` / `ttm_delete_giveup_total` / `d3_failover_total` 均不存在 |
| agent 的 S5 采集 | `gpuhealth_patch_counters_present 0`，`/debug` 的 `patch.present=false` |

agent 从上线第一天起就在如实报告这件事（S5 信号设计成"仅上报，不参与判定"，正是为了这个场景）。
计划里 **S5 一栏写的"仍未验收"不是疏漏，是这个事实的记录**。

## 录制：`tools/stress-recorder.py`

不靠看 dashboard。VictoriaMetrics 是 30 秒抓取，**结构上就量不了 60 秒级的断言**——
面板会把被测对象本身四舍五入掉。录制器直接打三个源：节点上 agent 的 `/debug`、guard 的
`/metrics`、apiserver，2 秒一行写 TSV。

三条刻意的设计：

- **失败也写行**。agent 打不通时写 `agent_ok=0` 而不是跳过这次采样。时间线上的空洞正是
  GPU hang 的样子，静默跳过等于把最重要的证据擦掉——那就是本项目要消灭的失效模式，高一层复现。
- **记 `kernts_*`（每条规则最后命中行的内核时间戳），不只记计数**。两列相减得到的是
  **内核里的区间**，不是区间加一个轮询周期。上表四个区间全是这种差值。
- **按栅格睡**，不是固定间隔睡。apiserver 慢一次不会让整条时间线漂移。

标记相位：另开一个 shell `echo "P1 vmfault dev0" > /tmp/stress/marker`，
文本落到下一行的 `marker` 列然后清空——注入时刻和观测用的是**同一个时钟**，
而不是人对着两个终端读表。

## 相位

`/work/faultgen <mode> <dev>` 与 `/work/killstorm.sh <rounds> <interval> single|all` 在 0024 的
`gpu-hang-stress` pod 内（特权，`nodeName` 钉死，挂 `/dev/kfd` + `/dev/dri`）。

| 相位 | 动作 | 想触发的环 | 预期 |
|---|---|---|---|
| P0 | 只录制，10 分钟 | — | 基线噪声：D 状态计数、exporter 时延、无内核致命行 |
| P1 | `faultgen vmfault 0` | GPU VM fault → KFD 队列驱逐 | 单卡异常；**大概率不产生 `killed entity`**，用来确认分级不会过度反应 |
| P2 | `faultgen spin 0` 在飞时 SIGKILL | **孤儿 fence**——8 月事故链的触发源 | `Trying to push to a killed entity`，然后 I2 窗口 |
| P3 | `killstorm.sh 5 60 all` | 批量误杀放大器（事故链第 ⑦ 环） | 多卡并发驱逐；这是历史上真正打死节点的形态 |
| P4 | 8 卡 × 3 worker 满载 2 小时 | 稳态 | 无注入下是否自发劣化 |

**P3 之后大概率需要整机重启**——D 状态杀不掉，fence 用户态 signal 不了（F11）。
用户已同意（"1 同意"），0024 是压测机，这是这轮的既定代价，不是意外。

每相位之间等到 `GPUUnrecoverable` 自行过期（15 分钟活动窗口）再开下一相位，否则条件粘在
True 上，I3 就没有起点了。

## 护栏

- guard 的 enforce 范围用 `GUARD_ENFORCE_NODES=wx-ms-w7900d-0024` 限死，0029/0043
  即使被判 L3 也只会落到 `not-in-enforce-scope`。这不是靠调容量下限做的——下限是**一个数**，
  低到能让 0024 通过就同时把 0029/0043 也放开了（见 `config.py` 的注释）。
- 0024 已 cordon，没有租户在上面。
- **不碰 0004/0005/0006**。

## 结果

（逐相位填写。未跑的相位不预填结论。）

### P0 基线（08:08:03 – 08:18，58 个采样点）

| 项 | 值 |
|---|---|
| agent / guard 可达 | 58/58 |
| exporter 抓取 | 8/8 健康，平均 51ms，最大 55ms |
| D 状态任务 | 0 |
| 内核致命计数器 | 全 0 |
| guard 判定 | 全程 `healthy` |

录制器自己在这一段里暴露了一个缺陷：guard 滚动后所有 `guard_*` 列变空，因为
重解析只挂在 *agent* 失败上。已改为两个端点各自失败各自重解析。
**每个端点必须能从自己的故障里恢复，共用一个触发条件等于让安静的那个一直坏着。**

### P1 单卡 VM fault（`faultgen vmfault 0`，08:19:01.570）

| 环节 | 时刻 | 间隔 |
|---|---|---|
| 注入 | 08:19:01.570 | — |
| 内核首条 VM fault 行 | 08:19:01.871 | **0.30 秒** |
| agent 计数器 `vm_fault` 0 → 4 | ≤ 08:19:07 | ~5 秒（一个采集周期内） |

**其余全部没动**：无 `killed entity`、无 hung task、D 状态 0、exporter 8/8、
无 condition 翻转、guard 全程 `healthy`。

→ **分级没有过度反应**。L1/L2/L3 的分离第一次在**真实故障**上被验证，
而不是在注入的日志行上。一张卡的 VM fault 不该摘掉一台 8 卡节点，它确实没有。

### P2 孤儿 fence（`faultgen spin 0` 在飞 158 秒后 SIGKILL，08:49:20.16）

**阴性，而且是有信息量的阴性。** 杀后 120 秒内：

| 观察项 | 结果 |
|---|---|
| `Trying to push to a killed entity` | **没有出现**（`sched_killed_entity` 计数仍为 0） |
| 内核日志里任何 amdgpu/drm/fence/ring 行 | 一条都没有 |
| D 状态任务 | 0 |
| Node Condition | 全 False |

→ **单个进程带着在飞的长核被 SIGKILL，不足以造出孤儿 fence。** 驱动把它干净地拆掉了。
8 月那条链要成立，还需要**并发的提交方**——即 entity 被拆的同时/之后仍有别的上下文
往它上面推 job。这正是 P3（killstorm：24 个 worker 同时被杀再重启）与 8 月事故
（平台层批量误杀）的形态，而不是 P2 的形态。

这条修正了协议表里 P2 那一行的预期：它写的是"孤儿 fence——8 月事故链的触发源"，
实际触发源是**批量**，不是**在飞**。I1/I2 的起点得从 P3 拿。

### P3 killstorm（08:53:38 – 08:58:59，5 轮 × SIGKILL 全部 24 个 worker）

24 个 worker（8 卡 × 3 进程，`ALLOC_GIB=14`），起手 VRAM 已经打到 0.2/48.0 GiB 剩余，
每轮把它们全部 SIGKILL 再重启。

| 观察项 | 结果 |
|---|---|
| `Trying to push to a killed entity` | **0 次** |
| hung task（`blocked for more than`） | **0 次** |
| D 状态 | 峰值 7 个任务、GPU 相关 3 个，**`stuck` 全程 0**，`max_seconds` 峰值 15.2 秒 |
| exporter | 全程 8/8 健康 |
| Node Condition | 全程 False |
| guard | 全程 `healthy` |

### P3b 并发 spin+vmfault（09:01:53 注入，09:02:05 SIGKILL）

四张卡（dev 0–3）同时跑 `faultgen both`（长核 + 非法地址解引用），在飞 12 秒后一起 SIGKILL，
背景仍有 24 个 worker 在churn。**同样阴性**：无 `killed entity`、无 hung task、
`stuck=0`、exporter 8/8、condition 全 False。`vm_fault` 计数停在 4 没有增长 ——
`both` 模式下长核占着引擎，进程在 vmfault 同步返回之前就被杀掉了，
所以这一轮连 P1 那个 VM fault 都没造出来。

### 三次升级都是阴性，这就是本轮压测的主要结论

| 相位 | 强度 | `killed entity` | hung task | 节点状态 |
|---|---|---|---|---|
| P1 | 单卡 VM fault | — | — | 健康 |
| P2 | 单进程在飞 SIGKILL | 无 | 无 | 健康 |
| P3 | 5 轮 × 24 进程满显存 SIGKILL | 无 | 无 | 健康 |
| P3b | 4 卡并发 spin+vmfault 在飞 SIGKILL | 无 | 无 | 健康 |

**在 6.14.14 + ROCm 30.10.2.0 上，8 月那条事故链无法按需复现。**
而 0024 自己在 2026-08-29 出过完整的一条（F12）。所以差异不在"杀得够不够狠"，
在别的地方 —— 候选：当时的驱动/ROCm 版本、当时的负载形态
（`llama-server` 那种长生命周期推理上下文，而不是本轮这种合成的分配-释放 churn）、
或者 pod/cgroup 层面的拆除（8 月是平台批量杀 **pod**，不是杀进程）。

对 A/B 的影响必须说清楚：**I1 与 I2 这两个区间，这轮拿不到。**
它们目前仍然只有 F12 那两个历史样本（0004 ≈1 分钟、0024 3 分 52 秒，相差近 4 倍）。
将来补丁装上之后，**没有可按需触发的对照臂**，就没法做成对实验 ——
这一条应当在决定装补丁之前解决，而不是之后。

拿到的是另一组同样有用的东西：**检测链在真实重负载下的行为基线**（见 P0/P1 与 P4）。
