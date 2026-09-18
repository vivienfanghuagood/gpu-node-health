# D1–D7 内核补丁：观测面与一个假阳性

这个目录不是补丁的家。补丁的工作树在 **`wx-ms-w7900d-0024:/usr/src/amdgpu-6.14.14-2226257.24.04/`**，
作者正在上面改。这里放两样东西：

1. `incident-export-20260916/` —— 2026-09-16 那版 diff 的快照，**已经落后于工作树**（见下）。
2. 本文 —— 用 0024 自己的日志做的一次核对，结论是 **D4 的 stale 计时器量错了东西**。

本仓库与补丁的关系只有一条：`agent/gpu_health_agent/signals/kernlog.py` 负责把补丁打出来的
每一条 printk 计数成一等指标，`tests/test_patch_contract.py` 把每条规则钉死在真实格式串上。

---

## 0. 先说方法：diff 和 `modinfo` 都不足以判断补丁在不在

判断"某个补丁在不在跑"，唯一算数的是**已加载模块的 `.rodata`**：

```sh
for m in amd-sched amdttm amdgpu; do echo "== $m"; zstdcat $(modinfo -n $m) | strings; done
```

这条方法不是讲究，是踩出来的：

- **补丁跨三个模块。** D1/D4 在 `amd-sched.ko`，D2/D7 在 `amdttm.ko`，D3/D5/D9-diag 在 `amdgpu.ko`。
  只查 `modinfo amdgpu` 会得出"一个都没装"的结论。本项目确实得出过，并写进了记录，是错的。
- **`/root/incident_export/` 的 diff 停在 2026-09-16，工作树比它新。** 至少三处只在工作树里：
  `d4_ctx_track.first_pending_jiffies`、`sched->ops->show_ring_state`（D9-diag）、
  `d4_ctx_lookup()` 里那条 D3.1 淘汰告警。照 diff 写监控规则会漏掉它们。
- **printk 的 `pr_fmt()` 前缀在 diff 里看不见。** TTM 那两条实际带 `[TTM] `。

各模块当前携带的补丁格式串：

| 模块 | 格式串 |
|---|---|
| `amd-sched.ko` | `D4 watchdog: fence context %llu stalled: …`、`D4 watchdog: map evicted pending-stalled context …`、`D1 remediate: force-signaled %d stalled fence(s) …` |
| `amdttm.ko` | `[TTM] ttm: BO %p delete blocked on unsignaled fences (attempt %d/%d)`、`[TTM] ttm: BO %p delete GIVING UP …` |
| `amdgpu.ko` | `amdgpu: ring %s state: wptr=… rptr=… fence emitted=%u signaled=%u fallback_timer=%s`（D9-diag，符号 `amdgpu_job_show_ring_state`） |

**`amdgpu.ko` 里没有任何 D3 failover 字符串。** `d3_pick_move_entity()` 任何路径都不打日志。
这一条单独要紧，见第 3 节。

---

## 1. D4 的 stale 量的是"上次完全空过"，不是"上次有进展"

### 代码

`sched_main.c`，两处写入 + 一处读取：

```c
/* d4_ctx_emit()，发射路径 */
if (t->emit_seq == t->done_seq)
        t->first_pending_jiffies = jiffies;   /* 只在队列空的时候打点 */

/* drm_sched_job_done()，完成路径 */
t->done_seq    = s_fence->finished.seqno;
t->done_jiffies = jiffies;
if (t->done_seq >= t->emit_seq)
        t->first_pending_jiffies = 0;          /* 只在彻底追平时清零 */

/* drm_sched_fence_watchdog()，判定 */
since = t->first_pending_jiffies ? t->first_pending_jiffies :
        (t->done_jiffies ? t->done_jiffies : t->emit_jiffies);
stale = jiffies > since ? jiffies - since : 0;
if (stale > DRM_SCHED_FENCE_WATCHDOG_PERIOD) { … }
```

注释写的意图是对的：*"a context that emits continuously but never completes still ages toward
the watchdog"*。问题在于实现分不开这两种情况：

- **只发不完成**（要抓的）：`emit_seq > done_seq` 恒成立
- **一边发一边完成，但手里始终留着在飞的活**（正常 GPU 租户）：`emit_seq > done_seq` 也恒成立

后者永远不满足 `done_seq >= emit_seq`，于是 `first_pending_jiffies` **开机打一次点，此后永不清零**，
`stale` 就等于这个负载自己的年龄，只增不减。

### 0024 自己的日志（2026-09-18T05:23:57 – 05:24:09，四张卡同时）

以 `0000:83:00.0` / context 1592 为例：

| 时刻 | D4 看到的 emitted | signaled | 差 | stale |
|---|---|---|---|---|
| 05:23:57.30 | 4219 | 4036 | **183** | 15s |
| 05:24:02.42 | 5548 | 5365 | **183** | 20s |
| 05:24:07.54 | 6876 | 6693 | **183** | 25s |

10.24 秒里 `done_seq` 前进了 **2657**（约 259 fence/s），在飞深度**恒定 183**，
而 `stale` 严丝合缝地每秒涨 1 秒。另外三张卡（`a3/c3/e3`，context 1913/2234/2555）
在飞深度**同样恰好是 183**。

**这个 context 不但没卡，它是这台机器上最忙的东西。** 它只是从来没有空到底过。

### 最硬的一条证据来自作者自己的 D9-diag

每条 D4 告警后面紧跟的那行是 `show_ring_state` 打的，同一瞬间的硬件视角：

```
D4:  fence context 1592 stalled: emitted=4219 signaled=4036 stale=15s
D9:  ring sdma0 state: wptr=0x00042fa0 rptr=0x00042f50 fence emitted=8515 signaled=8518 fallback_timer=armed
```

`wptr ≈ rptr`、`signaled ≥ emitted` —— **环已经排空，硬件手上没有未完成的活**。
D9-diag 加进来正是为了把"引擎跳过 / 完成丢失 / 中断丢失"区分开，
而它在这里给出的答案是第四种：**哪一种都不是，根本没有 stall**。
检测器和它自带的诊断钩子互相打脸，诊断钩子是对的。

### 后果不只是刷屏

```c
if (d4_remediate &&                                  /* 默认 1 */
    stale > msecs_to_jiffies(d4_remediate_ms) &&     /* 默认 30000 */
    stale > 2 * sched->timeout)
        d4_remediate_stalled_ctx(sched, t->context);
```

`d4_remediate_stalled_ctx()` 会把该 context 上所有未 signal 的 finished fence
**`dma_fence_set_error(-ECANCELED)` + `dma_fence_signal()`**。

在上面这个场景里，那就是对一个每秒完成 259 个 fence 的健康租户 context，
强制作废它手上的 183 个在飞 fence。这轮没发生，只因为 `stale` 涨到 27s 时机器就重启了，
**差 3 秒**。`2 * sched->timeout` 那道闸（SDMA 通常 10s → 20s）在 stale=20s 时就已经放行了，
当时唯一还拦着的就是 30s 这一个数。

补丁作者在那段注释里写了 *"remediation must never fire on possibly-live work"* ——
判断是对的，只是 `stale` 这个量本身没有兑现它。

历史上 D1 一共触发过 **2 次**，都在 2026-09-15T05:04:11，context 1263 / 1265，各 1 个 fence。
（此前本项目记过"D1 从未触发"，那是只翻了 journal 的当前几个 boot，`kern.log` 里有。）

### 建议的改法：换一下优先级

`done_jiffies` 已经在完成路径上每次都更新了，它正是"上次有进展"。

```diff
-		since = t->first_pending_jiffies ? t->first_pending_jiffies :
-			(t->done_jiffies ? t->done_jiffies : t->emit_jiffies);
+		/* 从"上次完成"起算，不从"上次由空转忙"起算。一个一边发一边完成、
+		 * 但手里始终留着在飞活的 context 永远不满足 done_seq >= emit_seq，
+		 * first_pending_jiffies 打一次点就再也不会清零，stale 于是变成
+		 * 负载自己的年龄。而真正从未完成过的 context，done_jiffies 仍是 0，
+		 * 自然落到 first_pending_jiffies，原本的意图不受影响。
+		 */
+		since = t->done_jiffies ? t->done_jiffies :
+			(t->first_pending_jiffies ? t->first_pending_jiffies :
+			 t->emit_jiffies);
```

覆盖两种情形：

| context | `done_jiffies` | 从哪起算 | 结果 |
|---|---|---|---|
| 一直有进展（流水线租户） | 每次完成都刷新 | 上次完成 | 毫秒级，永不触线 ✅ |
| 真卡住（要抓的） | 冻在卡住那一刻 | 上次完成 | 正常累积到阈值 ✅ |
| 从未完成过（僵尸） | 恒为 0 | `first_pending_jiffies` | 与现在一致 ✅ |

**在这条修掉之前，建议线上一律 `d4_remediate=0`（只观察不补救）。**
这也是 `docs/gpu-hang-stability-plan.md` 里 C2 早就写下的建议，现在有实测数据支撑了。

---

## 2. `d4_ctx_lookup()` 的淘汰策略：报了，但没拦住

128 槽满了之后按最小 `done_jiffies` 挑淘汰对象。从未完成过的 context `done_jiffies == 0`，
**永远是最小值，永远第一个被淘汰** —— 而那正是 D4 存在的理由。

工作树里已经加了 D3.1 的 `pr_warn_ratelimited("… map evicted pending-stalled context …")`，
它把这件事说出来了，**但淘汰照常执行**。压测时槽位被 CrashLoop churn 填满，
看门狗恰好在最该睁眼的时候瞎掉，只是这次瞎之前喊了一声。

建议：挑淘汰对象时跳过 `emit_seq > done_seq` 的条目，只在**全部 128 槽都 pending** 时才淘汰并告警。

（这条与第 1 节独立，但第 1 节修好之后它的触发面会小很多 —— 正常 context 不再长期 pending。）

---

## 3. `d3_pick_move_entity()` 全程不打日志

已从 `amdgpu.ko` 的 `.rodata` 确认：整个模块只有 D9-diag 一条补丁格式串，
**没有任何 D3 failover 字符串**。所以 D3 切到备用 SDMA entity 这件事在日志上不可见。

这不是"少个日志"。D3 的 failover 正是 D7 那条跨 context 丢 move fence 的前置条件
（`ttm_bo_move_accel_cleanup()` 里 `from->move->context != fence->context` 就直接"新的赢"），
也就是这套补丁里**唯一一条静默损坏显存、而不是明着挂住**的路径。

**这套补丁能造成的最危险的事，恰好是它唯一不上报的事。**

建议在返回备用 entity 的那条路径上加一条 `dev_warn_ratelimited`，把实例号打出来。
本仓库 `kernlog.py` 里的 `d3_failover` 规则已经写好在等它，
当前显式登记在 `tests/test_patch_contract.py` 的 `KNOWN_DEAD` 里 —— 
否则那个恒为 0 的计数会被当成"没发生过"。

---

## 4. `d3_ring_sched_stalled()` 无锁读 `d4_ctx_map`

`amd/amdgpu/amdgpu_ttm.c`，扫 128 个槽读 `context / emit_seq / done_seq / done_jiffies`，
**不持 `sched->d4_lock`**，而写侧（`drm_sched_job_done()` / `d4_ctx_emit()`）是持锁的。
字段之间可能撕裂，把健康 ring 读成 stalled。

误判的代价不是多一条告警，是**触发一次没必要的 D3 failover**，而 failover 就是第 3 节那条
静默损坏路径的前置条件。顺带两处：`jiffies > since + 2 * sched->timeout` 应为 `time_after()`；
命中后可以直接 `break`。

建议加 `spin_lock_irqsave(&sched->d4_lock, flags)`。代价是买卖路径上多一段
O(128) 关中断扫描 —— 把 `d4_ctx_map` 换成哈希表是另一件事，但正确性不该等它。

---

## 5. 复现与核对

```sh
# 补丁在不在、是哪一版（唯一算数的判据）
for m in amd-sched amdttm amdgpu; do zstdcat $(modinfo -n $m) | strings; done

# 当前生效的参数
grep . /sys/module/amd_sched/parameters/*

# 假阳性长什么样：找 stale 在涨、而 signaled 也在涨的那一对
grep -a "D4 watchdog: fence context" /var/log/kern.log | tail -20
# 每条后面紧跟的 D9 行给出同一瞬间的硬件视角
grep -aA1 "D4 watchdog: fence context" /var/log/kern.log | tail -40
```

判据一句话：**同一个 context 的 `signaled` 在涨，`stale` 也在涨 → 假阳性。**
真 stall 的 `signaled` 是不动的。

⚠️ 不要用 `dmesg`。本集群 uptime 最长 38 周，环形缓冲早就绕回，
8 月那次事故的证据只在 `kern.log*`（含 `.gz`）里。
