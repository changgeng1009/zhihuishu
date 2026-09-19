# 04 · MVP 范围与实施路线

> 阶段：第四步（定范围与路线）
> 原则：**先完成真实课程 MVP 能力测试，再决定是否重构现有开源代码**（用户目标 7）。

---

## 1. MVP 的成功判据（先写清楚，免得跑完才争论）

MVP 不是"功能都做完"，而是**证明下面 6 条在真实账号上成立**：

| # | 判据 | 怎么验 | 通过条件 |
|---|---|---|---|
| V1 | 能在真实课程上**列出课程与章节** | `list_courses` / `scan_tasks --json` | 输出的课程名与你账号里看到的一致，`adapter=zhs-browser` |
| V2 | 能**真实播放一个视频**并推动平台进度 | `run_video_tasks --course-id X --confirm` 只跑一章 | 平台侧进度条前进，`runs/*.raw.log` 里有上游进度行 |
| V3 | **任务点类型与状态**能结构化读出 | `scan_tasks --json` | `task_points[].type/status` 非 unknown；未完成数为真 |
| V4 | 弹题出现时能**读出题目与选项** | 观察 `answer_pending` 是否收到工单 | 工单里 `questions[0].stem` 与 `options[].text` 是真实题干 |
| V5 | **考试页一律不操作** | `safety_check` + 在考试页跑一次 `run_chapter --dry-run` | 返回 `EXAM_PAGE_BLOCKED`，且没有产生任何点击 |
| V6 | 断点可恢复 | `pause` → `resume` | 恢复后从断点章节继续，不重跑已完成任务点 |

**只有 V1–V6 全过，才进入"是否重构上游"的讨论。**

---

## 2. MVP 不做什么（明确划出去）

| 不做 | 原因 |
|---|---|
| 签到（C48–C51） | 未实测智慧树签到形态，**不声明**比声明了跑不通更诚实 |
| 直播 / 讨论任务（C16、C17） | 平台形态与风险未评估 |
| 图片题（C24） | 显式声明 `none` |
| 新形态课程的**写操作** | 上游无许可证，不复制其逻辑；自研需先实测页面 |
| 考试页的任何操作 | **红线** |

---

## 3. 真实课程 MVP 测试步骤（照抄即可）

> ⚠️ **前置**：所有动作都在**独立浏览器实例**里，不动你的日常浏览器。
> 见 `docs/03 §5.3` 与 `orchestrator/browser.py` 的隔离守卫。

### 步骤 0 · 环境

```bash
cd D:\CodexWork\智慧树刷课

# 0.1 上游就位（已 clone，此步只核对）
python -m orchestrator.cli upstreams
#     期望：4 个上游 pinned_commit 全部 OK、dirty 为空

# 0.2 建项目内 venv 并装 Autovisor 依赖（不污染系统 Python）
python -m venv .venv
.venv\Scripts\python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple ^
    httpx pillow playwright requests pygetwindow
.venv\Scripts\python -m playwright install chromium
```

### 步骤 1 · 登录（拿 cookie）

```bash
# 1.1 启动独立浏览器（双击 启动独立浏览器.cmd 亦可）
python -m orchestrator.browser --launch --url https://passport.zhihuishu.com/login

# 1.2 在弹出的窗口里手工登录（扫码或账密），然后回终端执行：
python -m orchestrator.cli cookies_login      # 等待并自动提取
python -m orchestrator.cli cookies_verify     # 真实请求一次平台，确认会话有效
python -m orchestrator.cli cookies            # 体检（值已掩码）
```

### 步骤 2 · 读侧验证（V1、V3）— **只读，零风险**

```bash
python -m orchestrator.cli list_courses
python -m orchestrator.cli scan_tasks --course-id <课程ID> --json
```

**看什么**：`adapter` 是否为 `zhs-browser`；`task_points` 是否非空；
`status` 里 `done/todo` 是否与你眼睛看到的一致。
**若为空或全 unknown**：这是 `docs/02 §3 缺口 2` 的已知情况 → 把
`runs/*.raw.log` 与 `scan_tasks --json` 发我，属于页面选择器要补，不是设计问题。

### 步骤 3 · 写侧验证（V2）— **有真实副作用，先 dry-run**

```bash
# 3.1 先看它会做什么（不产生写操作）
python -m orchestrator.cli run_chapter --course-id <课程ID> --chapter-id <章节ID> --dry-run

# 3.2 确认无误后，跑一个章节
python -m orchestrator.cli run_chapter --course-id <课程ID> --chapter-id <章节ID> --confirm

# 3.3 实时看断点与状态
python -m orchestrator.cli status --all
```

**看什么**：`runs/<req_id>.raw.log` 里有没有 Autovisor 的进度行；
平台侧进度是否前进；`status` 的 `completed_task_points` 是否在增长。

### 步骤 4 · 弹题验证（V4）

```bash
# 终端 A：启动本地 OpenAI 兼容代理（喂给上游的答题端点）
python -m orchestrator.cli shim-serve --port 8765

# 终端 B：抽题
python -m orchestrator.cli answer_pending
#   看到工单后回填（答案是选项下标或字母）：
python -m orchestrator.cli answer_submit --ticket-id tk_xxx --answers "A"
python -m orchestrator.cli answer_stats
```

**看什么**：`answer_pending` 里 `questions[0].stem` 是否是真题干。
若是 `raw_prompt` 有内容而 `questions` 为空 → 结构化解析失败但**原文保留**（设计如此），
把原文发我补选择器。

### 步骤 5 · 考试红线验证（V5）— **最重要的验收**

```bash
# 5.1 静态裁决
python -m orchestrator.cli safety_check --url "https://examloop.zhihuishu.com/exam" --action answer
#     期望：页面类别 exam_blocked | 裁决 deny → 拒绝

python -m orchestrator.cli safety_check --url "https://studywisdomh5.zhihuishu.com/exam" --action answer
#     期望：页面类别 practice | 裁决 allow → 允许

# 5.2 在真实考试页上跑 dry-run（应被拦下，且不产生任何点击）
#     打开考试页，取当前 URL，然后：
python -m orchestrator.cli run_chapter --course-id <考试课程ID> --chapter-id <任意> --dry-run
#     期望：state=failed，error.code=EXAM_PAGE_BLOCKED，next_actions 提示人工完成
```

### 步骤 6 · 断点验证（V6）

```bash
python -m orchestrator.cli run_chapter --course-id <课程ID> --chapter-id <章节ID> --confirm
# 跑到一半时（另一个终端）：
python -m orchestrator.cli pause  --request-id <req_id>
python -m orchestrator.cli status --request-id <req_id>     # state=paused，含断点
python -m orchestrator.cli resume --request-id <req_id> --confirm
```

---

## 4. 路线图

| 阶段 | 内容 | 状态 | 依赖 |
|---|---|---|---|
| **M0** | 统一层复用 + Adapter 骨架 + 考试守卫 + 67 项离线测试 | ✅ **完成** | 无 |
| **M1** | 真实课程读侧：登录 → `list_courses` → `scan_tasks` | ⏳ 待你的账号 | 步骤 1–2 |
| **M2** | 真实视频：`run_chapter` 跑通一章（V2） | ⏳ | 步骤 3 |
| **M3** | 弹题 Agent 链路实跑（V4） | ⏳ | 步骤 4 |
| **M4** | 掌握度 / 普通练习 / 章节练习自动作答 | ⏳ | M3 稳定后 |
| **M5** | 新形态课程资源（先只读，写操作另议） | ⛔ 许可证阻塞 | 见 §5 |

---

## 5. 什么情况下才考虑"重构现有开源代码"

用户原则是**优先复用、最小改造、禁止为了代码干净而重写**。
因此重构必须有硬理由。以下是**仅有的三种**可接受理由：

| 理由 | 触发条件 | 允许的手段 |
|---|---|---|
| ① 上游失效且无替代 | `upstreams` 项目停更超过 6 个月，或契约被破坏且无 fork | fork 一份并锁住自己的 commit；**仍不并入本仓库** |
| ② 能力客观缺失 | 上游确实没有该能力（如"弹题读题作答"） | 在 `zhs-browser` 里**自研最小实现**，不重写上游已做的部分 |
| ③ 许可证不允许 | 如 `resource-helper` 无 LICENSE | 自研等价实现，或申请作者授权 |

**明确不允许的理由**：
- "代码不好看"/"想统一风格"
- "上游写得复杂，我想简化"
- "反正我有空"

`docs/02 §3` 的 3 个缺口是目前**唯一**已知符合理由 ②/③ 的地方。
除它们之外，MVP 阶段**不应改动任何上游代码**，`cli upstreams` 会一直盯着。

---

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| 平台改版导致选择器全失效 | 每个 Adapter 的 `degrade_on` 含 `platform_changed`；解析失败降级为"能报进度不报细节"，不崩 |
| 风控封号 | 账号级限流（`min_interval_ms`）+ 8 类错误里 `risk_control` 唯一触发全局熔断且**不 fallback** |
| 误在考试页操作 | **双层校验 + 禁止 fallback + 23 项测试**（见 `docs/03 §7`） |
| 两套浏览器互抢账号会话 | `max_concurrency = 1` + manifest 里显式写明互斥 |
| 上游依赖装到系统 Python | worker 解释器优先级 `ZHS_PYTHON` → 项目 `.venv` → 当前解释器；文档明确要求项目内 venv |
