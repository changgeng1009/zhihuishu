# 03 · 统一接口与 Adapter 规范（智慧树）

> 阶段：第三步（设计统一接口和 Adapter 规范）
> 本文是**编码阶段的契约文件**。任何 Adapter 满足本文契约即可被统一层调用，不需要改统一层代码。
>
> **接口来源**：正文 §2–§6 的 Envelope / 状态机 / 错误分类 / 命令表**完全沿用**
> 姊妹项目 `cx`（学习通）与 `zhihui`（智慧职教）的定义——这是用户目标 6
> "对外接口保持一致"的落地点。**§7 的考试守卫与 §8 的页面路由是智慧树独有的新增**。

---

## 1. 分层架构

```
User / DeepSeek Agent / MCP Client / 其他 Agent
                 │
      ┌──────────┴──────────┐
      │  Unified API / MCP  │   ← 命令层（29 条统一命令，与 cx/zhihui 同名）
      └──────────┬──────────┘
                 │
      ┌──────────┴──────────┐
      │    ExamGuard ★      │   ← 智慧树新增：考试/监考拦截（写操作前置）
      └──────────┬──────────┘
                 │
      ┌──────────┴──────────┐
      │     Task Router     │   ← 选路 + fallback + 限流 + 状态机
      └──────────┬──────────┘
                 │
      ┌──────────┴──────────┐
      │ Capability Registry │   ← 51 项能力声明（C01–C51），manifest 驱动
      └──────────┬──────────┘
                 │
      ┌──────────┴──────────┐
      │   Adapter Layer     │   ← 每个上游一个 Adapter，进程/浏览器隔离
      └──────────┬──────────┘
                 │
   ┌─────────────┼──────────────┬────────────────┐
   │             │              │                │
 zhsh-autovisor zhsh-browser  (OCS 知识)      mock
  subprocess     CDP 浏览器    只落选择器      故障注入
  (MIT)          (自建)        (MIT/复制)
```

**★ ExamGuard 的位置是有意的**：它在 Router **之上**。
理由是考试红线**与选哪个 Adapter 无关**——只要目标页面是考试页，
无论谁来执行都必须拒绝。放在 Router 之下，就会出现"某个 Adapter 忘了判断"的漏洞。

**四条不可违反的约束**（沿用 cx，红线 R5 为本项目新增）：

| # | 约束 | 原因 |
|---|---|---|
| R1 | Adapter 只能通过**进程/网络/浏览器/文件边界**与第三方交互 | 许可证传染性隔离 |
| R2 | Adapter 不得修改 `upstreams/` 下任何文件 | 保证可随时按 commit 重拉 |
| R3 | 统一层不得包含任何第三方业务逻辑的复制 | 可独立演进 |
| R4 | 单个 Adapter 失效不得影响其他 Adapter | 可单独替换 |
| R5 | **一切产物只落盘在本项目目录内**（`D:\CodexWork\智慧树刷课`） | 用户硬规则 |

---

## 2. 统一响应封装（Envelope）

**所有**命令返回同一种信封。字段与 `cx`/`zhihui` **逐字一致**：

```jsonc
{
  "ok": false,
  "command": "run_chapter",
  "request_id": "req_20260919_0031_a1f3",
  "account": "acc_01",
  "adapter": "zhs-autovisor",          // ★ 实际服务该请求的 Adapter
  "adapter_version": "40988f39",       // 上游 commit sha（可追溯）
  "state": "blocked",
  "started_at": "2026-09-19T00:31:02+08:00",
  "finished_at": "2026-09-19T00:34:18+08:00",
  "duration_ms": 196000,
  "data": null,
  "warnings": ["3 个任务点因未开放被跳过（策略=continue）"],
  "error": {
    "code": "EXAM_PAGE_BLOCKED",       // ★ 智慧树新增的稳定错误码
    "category": "permission",
    "message": "拒绝在考试/监考页面执行自动操作：.../doexamination（共享课正式考试）",
    "retryable": false,
    "adapter_raw": "…………原始 stderr 片段…………"
  },
  "fallback_trace": [
    { "adapter": "zhs-browser",  "capability": "C12", "ok": false,
      "error_code": "ADAPTER_NOT_READY", "elapsed_ms": 812 },
    { "adapter": "zhs-autovisor", "capability": "C12", "ok": true,
      "elapsed_ms": 195188 }
  ],
  "next_actions": ["该页面为正式考试，请人工完成"]
}
```

### 2.1 错误分类（`error.category`）— 8 类，与 cx/zhihui 一致

| category | 含义 | Router 行为 | 置 `blocked` |
|---|---|---|---|
| `transient` | 网络抖动、超时、5xx | 同 Adapter 退避重试 → 再 fallback | 否 |
| `auth` | 会话失效、密码错误 | 触发重登 → 重试一次 → `needs_manual_action` | 否 |
| `permission` | 无权限、**考试页拦截** | 直接 fallback，不重试 | 否 |
| `not_supported` | 该 Adapter 不具备此能力 | 立即 fallback | 否 |
| `platform_changed` | 平台改版导致解析失败 | 标记 Adapter 降级 → fallback | 否 |
| `risk_control` | 风控/限流/验证码墙 | **立即全局熔断**，不 fallback | **是** |
| `input` | 参数错误 | 不重试，直接返回 | 否 |
| `internal` | 统一层自身 bug | 不重试，记录 | 否 |

> ⚠️ **`EXAM_PAGE_BLOCKED` 归 `permission` 而不是新增第 9 类**，且**不 fallback 到其他平台无关 Adapter**。
> 但要注意：`permission` 在 Router 的默认行为是"直接 fallback"。
> 若本平台的另一个 Adapter 也会落到同一考试页，fallback 无意义——
> 因此 **ExamGuard 在 Router 之上拦截，直接在命令层返回，不进入 fallback 流程**。

---

## 3. 对外命令规范（与 cx/zhihui 同名，29 条）

### 3.1 读命令（幂等）

| 命令 | 参数 | 返回 `data` | 能力 |
|---|---|---|---|
| `list_courses` | — | `[{course_id, clazz_id, cpi, name, teacher, fid}]` | C06 |
| `get_course` | `course_id` | `{...元数据, chapter_count, task_point_count}` | C07 |
| `get_progress` | `[course_id]` | `{overall:{done,total,ratio}, courses:[...]}` | C10,C11 |
| `scan_tasks` | `course_id` `[chapter_id]` `[types]` | `{task_points:[{id,chapter_id,chapter_name,type,title,status}], summary:{...}}` | C08,C09 |
| `get_homework` | `[course_id]` | `{homework:[...], deadlines:[...]}` | C29 |

`types` 取值：`video｜audio｜document｜ppt｜reading｜live｜discussion｜quiz`
`status` 取值：`done｜todo｜locked｜unknown`

### 3.2 写命令（非幂等，需 `--confirm`）

| 命令 | 参数 | 说明 | 能力 |
|---|---|---|---|
| `run_course` | `course_id` `[--dry-run]` `[--types ...]` | 跑完一门课全部未完成任务点 | C12–C21 |
| `run_chapter` | `course_id` `chapter_id` `[--dry-run]` | 只跑指定章节 | C12–C21 |
| `run_video_tasks` | `course_id` `[chapter_id]` | 只跑视频类 | C12 |
| `run_reading_tasks` | `course_id` `[chapter_id]` | 只跑阅读/文档类 | C14,C15 |

> **智慧树补充约束**：以上四条写命令在执行前**必须**逐页过 `safety.assert_writable()`。
> `--dry-run` 也必须走守卫（提前暴露"这门课的目标章节里混着考试页"）。

### 3.3 控制命令
`status` `pause` `resume` `retry` `stop` — 语义与 cx/zhihui 一致。

### 3.4 辅助命令
`adapters` `probe` `accounts`

### 3.5 Agent 答题链路（目标 3 / 4 的落地）
| 命令 | 参数 | 语义 | 能力 |
|---|---|---|---|
| `answer_pending` | `[--timeout S]` `[--limit N]` | 取出待答工单（长轮询） | C43,C47 |
| `answer_submit` | `--ticket-id X` `--answers JSON` | 回填答案 | C44,C45 |
| `answer_stats` | — | 待答/已答/超时/丢弃计数 | C47 |

### 3.6 智慧树新增辅助命令
| 命令 | 说明 |
|---|---|
| `safety` | 打印考试/监考规则表（`safety.audit_table()`），人工可核 |
| `safety_check` | `--url X [--action play]` 单 URL 裁决，用于排障 |
| `upstreams` | 打印 `upstreams.lock.json` 并与实际 HEAD 比对 |

---

## 4. 统一任务状态机（8 态）

与 cx/zhihui **完全一致**：`pending` `running` `completed` `failed` `blocked`
`needs_manual_action` `paused` `cancelled`。

智慧树语境下的语义微调：

| 状态 | 智慧树语境 |
|---|---|
| `blocked` | 滑块验证墙 / 平台风控 |
| `needs_manual_action` | **课中弹题超时未答**（默认策略）、人脸/点选验证码、未知页面 |
| `completed` | 目标任务点全部完成（**以平台进度为准**，不以本地计时为准） |

断点持久化结构同 cx `accounts/{id}/state.json`。

---

## 5. Adapter 契约

### 5.1 Manifest（声明式，JSON）

新增字段 `platform` 与 `page_scope`（智慧树独有，因为一个平台有 8+ 种学习页）：

```jsonc
{
  "id": "zhs-autovisor",
  "name": "CXRunfree/Autovisor Adapter（视频写侧主力）",
  "kind": "subprocess",
  "platform": "zhihuishu",              // ★ 新增：平台标识
  "enabled": true,
  "priority": 10,
  "upstream": {
    "repo": "https://github.com/CXRunfree/Autovisor",
    "path": "upstreams/Autovisor",
    "pinned_commit": "40988f393f38881e86f5a32e46d8d93dc849155e",
    "license": "MIT",
    "isolation": "process"
  },
  "runtime": {
    "language": "python",
    "version": ">=3.10,<3.14",
    "entry": "Autovisor.py"
  },
  "page_scope": [                        // ★ 新增：本 Adapter 只在这些页面工作
    "studyvideoh5.zhihuishu.com",
    "studyplush5.zhihuishu.com",
    "fusioncourseh5.zhihuishu.com",
    "studywisdomh5.zhihuishu.com",
    "wisdom-mooc.zhihuishu.com"
  ],
  "capabilities": {
    "C12": {"level": "full", "op": "run_video", "params": ["speed", "limit_max_time"]},
    "C18": {"level": "full", "op": "run_video"},
    "C20": {"level": "full", "params": ["speed"], "max": 1.8},
    "C24": {"level": "none"},
    "C43": {"level": "partial", "note": "能检测到弹题弹窗，但读不到结构化题干 → 依赖 DOM 通道"}
  },
  "health": {
    "probe": {"method": "check_course", "args": {"dry_run": true}, "timeout_ms": 60000},
    "degrade_on": ["platform_changed", "risk_control"]
  },
  "limits": {"max_concurrency": 1, "min_interval_ms": 3000}
}
```

### 5.2 运行期接口

同 `cx` 的 `adapters/base.py`：`setup` / `probe` / `supports` / `invoke` / `cancel` / `teardown`，
其中只有 `invoke` 必须实现。**额外硬性要求（智慧树）**：

```python
def invoke(self, capability_id, params, ctx) -> AdapterResult:
    # ★ 写能力必须在最前面过守卫，且守卫失败要抛 ExamBlockedError
    if capability_id in WRITE_CAPABILITIES:
        safety.assert_writable(
            url=params.get("url") or self.current_url(ctx),
            action="play",
            page_text=self.visible_text(ctx),   # 运行期 DOM 复核
        )
    ...
```

### 5.3 浏览器通道（kind = `browser`）

智慧树的视频与弹题是 DOM 操作，因此新增一类 kind：

| kind | `invoke` 做什么 |
|---|---|
| `subprocess` | 生成 `config.ini` → `subprocess.Popen` → 流式解析 stdout → 退出码判定 |
| `mcp` | MCP stdio 客户端 → `tools/call` |
| `http` | requests 调 endpoint |
| **`browser`** | **CDP（`orchestrator/cdp.py`，零依赖手写）→ 注入脚本 → `Runtime.evaluate` 取结构化结果** |

浏览器隔离沿用 cx 的约定（`browser.py` + `assert_isolated()`）：
独立 profile `.browser/edge-profile`、CDP 端口 **9333**、仅监听 `127.0.0.1`。
**绝不附着到用户日常浏览器，绝不批量杀 `msedge.exe`。**

---

## 6. 统一日志规范

`runs/{request_id}.jsonl` 一行一事件，字段与 cx **逐字一致**
（`ts/level/event/request_id/adapter/account/course/chapter/task_type/state_from/state_to/attempt/ok/error/extra`）。

智慧树新增两个事件：

| event | 触发时机 |
|---|---|
| `safety.blocked` | ExamGuard 拒绝了一次写操作（含 URL、action、命中的规则） |
| `safety.manual_required` | 未知页面或无 DOM 文本，转人工 |

`runs/{request_id}.raw.log` 保留 Autovisor 的**原始 stdout/stderr**（脱敏后）。
这是排障的唯一手段——Autovisor 的失败原因全在它的中文日志文本里。

---

## 7. ★ 考试 / 监考守卫规范（智慧树独有，目标 5）

实现：`orchestrator/safety.py`。规则表与理由见 `docs/01 §3` 与 `docs/02`。

### 7.1 判定顺序（不可调整）

```
① 练习白名单命中  → PRACTICE     允许自动作答
② 考试黑名单命中  → EXAM_BLOCKED 拒绝一切写操作
③ 学习页命中      → LEARNING     允许播放/切章
④ 其余            → UNKNOWN      只读放行；写操作 → needs_manual_action
```

`/exam` 歧义（`examloop` 是考试、`studywisdomh5` 是练习）要求**域名 + 路径联合匹配**，
且白名单优先——顺序调换会导致掌握度练习被误杀或正式考试被误放。

### 7.2 双层校验

- **第一层（静态）**：URL 正则分类。
- **第二层（运行期）**：Adapter 传入**已渲染页面的可见文本**，
  用 `DOM_EXAM_MARKERS`（考试须知/监考/诚信考试承诺/切屏将被记录/人脸识别/期中期末考试…）二次确认。
  **写操作必须提供页面文本**，未提供则判 `MANUAL`。

### 7.3 拒绝时的行为（硬性）

1. 返回 `error.code = "EXAM_PAGE_BLOCKED"`、`category = "permission"`
2. `next_actions` 给出"请人工完成"的指引
3. **不得 fallback 到其他 Adapter**（同一页面，换谁执行都是违规）
4. **不得进入重试队列**
5. 写一条 `safety.blocked` 审计日志

---

## 8. ★ 页面路由规范（智慧树独有）

一个平台 8+ 种学习页布局，Adapter 必须**声明自己负责哪些页面**（manifest `page_scope`），
Router 据 URL 选路。这样"某个页面改版"只影响一个 Adapter。

```
                     URL
                      │
        ┌─────────────┴─────────────┐
        ▼                           ▼
  safety.classify()           page_scope 匹配
        │                           │
   EXAM_BLOCKED ──► 拒绝       选中有该域的 Adapter
        │                           │
   PRACTICE/LEARNING ──────────────►│
                                    ▼
                             fallback 链执行
```

**未识别页面**：不路由给任何 Adapter，直接 `needs_manual_action`。
新增页面类型的正确做法 = 加一个 Adapter 或扩一个 `page_scope`，**不是**在现有 Adapter 里塞 if。

---

## 9. 目录结构

```
智慧树刷课/
├─ docs/                          # 01–05
├─ orchestrator/                  # ★ 统一层（本项目唯一自研代码）
│  ├─ safety.py                   # ★ 考试/监考守卫（智慧树独有）
│  ├─ models.py / errors.py / capabilities.py / registry.py / router.py
│  ├─ state.py / throttle.py / risk.py / structured_log.py / redact.py
│  ├─ session.py / control.py / answer_broker.py / openai_shim.py
│  ├─ browser.py / cdp.py / cookies.py / session_verify.py
│  ├─ services.py / cli.py / mcp_server.py / bootstrap.py / fixtures.py
│  └─ adapters/
│     ├─ base.py / mock.py
│     ├─ zhs_dom.py               # ★ OCS(MIT) 选择器知识（保留声明）
│     ├─ zhs_autovisor.py         # ★ 视频写侧主力
│     ├─ zhs_browser.py           # ★ 弹题/掌握度/练习通道
│     ├─ zhs_worker.py            # ★ subprocess 胶水
│     └─ manifests/*.json
├─ upstreams/                     # gitignore｜只读 clone（红线 R2）
├─ upstreams.lock.json            # ★ 锁 commit + 许可证（入库）
├─ accounts/{id}/                 # gitignore｜账号工作区
├─ runs/                          # gitignore｜日志 + 答题工单
├─ tests/                         # 离线可跑
└─ pyproject.toml
```

### 复用说明（对应用户原则"不要从零重写"）

`orchestrator/` 中**平台无关**的 21 个模块**整体复用自 `cx` 项目**（models/errors/
capabilities/registry/router/state/throttle/risk/structured_log/redact/session/control/
answer_broker/openai_shim/services/cli/mcp_server/bootstrap/browser/cdp/cookies/session_verify/
answer_broker/fixtures），改造仅限于：Adapter 注册表、Cookie 域名后缀、
风控登录特征、CLI/MCP 文案。**新增的只有** `safety.py` 与 4 个 `zhs_*` 文件。
