# 智慧树自动化能力平台

> ## ⚠️ 免责声明
>
> 1. 本项目**仅供学习与技术研究**，用于了解自动化架构设计、进程隔离、Adapter 模式等工程实践。
> 2. 本项目与任何第三方平台、机构、组织**没有任何关联**；引用的开源代码版权归其原作者所有，均已按其许可证要求使用（见 [LICENSE](LICENSE) 与 `upstreams.lock.json`）。
> 3. 本项目**不提供任何形式的服务或保证**；使用者因使用、滥用或误用本项目产生的一切后果，由使用者自行承担，**与作者无关**。
> 4. **本项目在正式考试、期中、期末、监考页面上一律不执行任何自动操作**（见 §考试红线）。
> 5. 请遵守所在学校/单位的相关规定以及当地法律法规。**下载或复制本项目即视为已阅读并同意本声明。**

把多个**独立第三方**智慧树工具，变成一个可以被**用户 / Agent / MCP Client 统一调用**的自动化能力平台。

统一层不重写任何第三方逻辑，也不修改它们的源码——每个第三方项目作为一个独立 Adapter，
通过 **进程 / 浏览器 / 文件边界**接入。某个项目失效时，单独换掉它的 Adapter 即可。

统一层本身**零第三方依赖**（全部 stdlib），因为"可审计、可离线自证"比"功能多"更重要。

---

## 当前状态

| 阶段 | 状态 | 说明 |
|---|---|---|
| 第一步 · 上游生态分析 | ✅ 完成 | [`docs/01`](docs/01-上游生态分析报告.md)：4 个上游的许可/形态/能力逐条核实 |
| 第二步 · 能力矩阵 | ✅ 完成 | [`docs/02`](docs/02-能力矩阵与缺口.md)：51 项能力覆盖盘点，3 个缺口写清 |
| 第三步 · 统一接口规范 | ✅ 完成 | [`docs/03`](docs/03-统一接口与Adapter规范.md)：与「学习通」「智慧职教」两个姊妹项目接口对齐 |
| **M0 · 统一层 + Adapter** | ✅ **完成** | 67 项测试；29+3 条命令可跑；**不需要账号、不需要网络** |
| **考试守卫** | ✅ **完成并测试** | `safety.py` 30 条规则、双层校验、禁止 fallback、`cli safety` 可人工核对 |
| M1 · 真实课程 MVP | ⏳ 待联调 | 需要你的账号：登录 → `scan_tasks` → 跑 1 个视频 |
| M2 · 弹题 Agent 链路实跑 | ⏳ 待联调 | 链路代码就位，需真实弹题页面验证 |
| M3 · 掌握度/练习自动作答 | ⏳ 待联调 | 同上 |
| M4 · 新形态课程写操作 | ⛔ 阻塞 | 上游 `resource-helper` **无许可证**，不复制其逻辑；改自研需先实测页面 |

> **诚实标注**：`zhs-browser` 与 `zhs-autovisor` 两个 Adapter 已注册、能力已声明、
> 守卫已生效，但**尚未在真实账号上跑通完整课程**（M1）。判据永远看输出里的
> `adapter=` 字段与 `warnings`——`adapter=mock` 即假数据。

---

## 考试红线

用户要求：**正式考试、期中、期末、监考页面一律不自动操作。**

这条不是"注意别写错"，而是统一层的一段代码（`orchestrator/safety.py`），
配合 30 条规则与两层校验，并由 `tests/test_safety.py` 钉死。

**为什么必须放在统一层**：智慧树的 URL 里 `/exam` 同时出现在语义**完全相反**的两类页面上——

| URL | 实际是什么 | 处置 |
|---|---|---|
| `examloop.zhihuishu.com/exam` | 正式考试 | 🚫 拒绝 |
| `studywisdomh5.zhihuishu.com/exam` | 掌握度练习 | ✅ 允许作答 |

只要有一个 Adapter 少判断一层，就可能把"自动答题"用在正式考上。
所以判定顺序被固定为（**不可调整**）：

```
① 练习白名单命中  → PRACTICE     允许自动作答
② 考试黑名单命中  → EXAM_BLOCKED 拒绝一切写操作，且不 fallback、不重试
③ 学习页命中      → LEARNING     允许播放/切章
④ 其余            → UNKNOWN      只读放行；写操作一律转人工
```

**第二层校验**：URL 是静态的，课程改版后可能失效。所以写操作前，Adapter 必须把
**页面已渲染的可见文本**交给守卫复核——出现「考试须知 / 监考 / 诚信考试承诺 /
切屏将被记录 / 人脸识别 / 期中考试」等 12 个特征词，即使 URL 命中了白名单也一律拒绝。

人工随时可核：

```bash
python -m orchestrator.cli safety                       # 打印全部规则
python -m orchestrator.cli safety_check \
    --url "https://examloop.zhihuishu.com/exam" \
    --action answer --page-text "期末考试"
#   → 页面类别 exam_blocked | 裁决 deny → 拒绝
```

---

## 快速开始

**零第三方依赖**（stdlib only，不需要 `pip install` 任何东西）。

```bash
# 查看已注册的 Adapter（含许可证与页面范围）
python -m orchestrator.cli adapters

# 能力覆盖情况
python -m orchestrator.cli capabilities

# ★ 考试守卫规则表 / 单 URL 裁决
python -m orchestrator.cli safety
python -m orchestrator.cli safety_check --url <URL> --action read

# ★ 核对上游锁（红线 R2 的可执行检查）
python -m orchestrator.cli upstreams

# 跑测试（全离线）
python tests/run_tests.py -v
```

加 `--json` 得到机器可读的完整 Envelope；加 `-v` 把结构化日志打到 stderr。

---

## 架构

```
User / Agent / MCP Client
              │
   统一 API / MCP 服务        29 + 3 条命令
              │
    ★ ExamGuard            考试/监考拦截（在 Router 之上，与选哪个 Adapter 无关）
              │
        Task Router         选路 · fallback · 限流熔断
              │
    Capability Registry     51 项能力（C01–C51），manifest 驱动
              │
       Adapter Layer        进程 / 浏览器隔离，禁止 import 上游
              │
   ┌──────────┼──────────────────┬──────────────┐
zhs-browser  zhs-autovisor    (OCS DOM 知识)   mock
 CDP 浏览器   subprocess        只落选择器     故障注入
 (自建)       (MIT)             (MIT/复制)
```

**为什么 `zhs-browser` 优先级更高（priority=10 < 20）**：智慧树的读侧
（课程/章节/任务点/进度/作业）**必须**在已登录页面里读——Autovisor 是
"给课程链接就跑"的独立程序，没有"列出我账号下有哪些课"的接口。
写侧（长时间连续播放）则交给 Autovisor，它在这方面已经过三年打磨。

两者**互斥**（都会驱动浏览器），由 `limits.max_concurrency = 1` 与账号级限流保证。

---

## 两个 Adapter 的分工

| | `zhs-browser`（priority 10） | `zhs-autovisor`（priority 20） |
|---|---|---|
| 形态 | CDP 浏览器通道（自建，零依赖） | 子进程（MIT） |
| 上游 | 复制 `ocsjs` 的**选择器知识**（MIT） | 调用 `CXRunfree/Autovisor`（MIT） |
| 强项 | 读课程/章节/任务点；**弹题读题与作答**；掌握度/练习 | 视频播放、章节切换、进度检测、停滞保护、弹题期间暂停 |
| 弱项 | 长时间连续播放不如上游打磨 | **读侧没有编程接口**；弹题只能暂停不能作答 |
| 声明不支持 | `C24` 图片题（显式声明，Router 可据此换 Adapter） | `C26` 章节测验、`C30` 考试（安全红线） |

---

## 弹题：本项目的增量

**上游怎么做的**：

- OCS：`Math.floor(Math.random() * options.length)` —— **随机选一个**
- Autovisor：**暂停播放并等待**，把人叫回来

**本项目怎么做的**（目标 3、4）：

```
课中弹题弹窗出现
      │
      ▼  zhs-browser 读 DOM（三种弹窗形态的选择器来自 OCS）
  结构化题目 {stem, options[]}
      │
      ▼  写入待答工单 runs/answer/（answer_broker）
  操控 Agent / LLM 作答
      │
      ▼  回填 answer_submit → 注入页面点击 → 提交 → 关闭弹窗
  继续播放
      │
      └─ 超时未答 → 默认【暂停转人工】，绝不随机作答
```

**关键取舍：超时默认不随机作答。** 随机选会在掌握度与练习里留下错答记录，
而"暂停转人工"最坏只是慢。**宁可慢，不可错。**

三种弹窗形态（`zhs_dom.POPUPS`，来自 `ocsjs/zhs.ts`）：

| 页面 | 弹窗根节点 | 选项 |
|---|---|---|
| 经典共享课 | `#playTopic-dialog` | `.topic .radio ul > li` |
| 新共享课 | `.ai-test-question-wrapper` | `.options .option` |
| 新智慧 / AI 助教 | `.ai-class-exercise-dialog` | `.ques-list .item .option` |

---

## 四条不可违反的红线

| # | 红线 | 原因 |
|---|---|---|
| R1 | Adapter 只能通过**进程/浏览器/文件边界**与第三方交互 | 许可证传染性隔离 |
| R2 | 不得修改 `upstreams/` 下任何文件 | 保证可随时按 commit 重拉；`cli upstreams` 可执行检查 |
| R3 | 统一层不得包含任何第三方业务逻辑的复制 | 可独立演进 |
| R4 | 单个 Adapter 失效不影响其他 Adapter | 可单独替换 |
| **R5** | **一切产物只落盘在本项目目录内** | 你的硬规则 |

R1 由静态检查守卫：统一层代码里不允许出现 `import ocsjs` / `sys.path.insert` 这类写法。
`zhs_dom.py` 是**唯一**的例外，且它是**数据**（选择器常量）不是**流程**，并且保留了 MIT 版权声明。

### 许可证状态

| 项目 | 许可证 | 接触方式 |
|---|---|---|
| `ocsjs/ocsjs` | **MIT** | ✅ 复制选择器知识（保留声明） |
| `CXRunfree/Autovisor` | **MIT** | ✅ subprocess |
| `Cooanyh/zhihuishu-zhangwodu` | CC BY-NC-SA 4.0 | ⚠️ **仅只读参考**（NC 限非商用 + SA 传染） |
| `Finn-Holmes/zhihuishu-resource-helper` | **无 LICENSE** | ⚠️ **仅只读参考**（默认保留所有权利） |

---

## 可观测性

每个请求产生三份日志：

| 文件 | 内容 |
|---|---|
| `runs/<req_id>.jsonl` | 结构化审计日志：Adapter / 账号 / 课程 / 章节 / 任务类型 / 起止 / 成败 / 错误 / fallback |
| `runs/<req_id>.raw.log` | **上游的原始 stdout/stderr**（脱敏后）。Autovisor 的失败原因全在中文日志里，这是唯一能定位的手段 |
| stdout | 人读摘要 |

智慧树新增两个审计事件：`safety.blocked`（守卫拒绝了一次写操作）、
`safety.manual_required`（未知页面转人工）。

凭据脱敏是**强制环节**：手机号、密码、cookie、token 在落盘前统一掩码。

---

## 目录结构

```
docs/                    01 上游分析 · 02 能力矩阵 · 03 接口规范 · 04 MVP 路线
orchestrator/            统一层（本仓库唯一自研代码）
  safety.py              ★ 考试/监考守卫（最重要的一段）
  models.py / errors.py / capabilities.py / registry.py / router.py
  state.py / throttle.py / risk.py / structured_log.py / redact.py
  session.py / control.py / answer_broker.py / openai_shim.py
  browser.py / cdp.py / cookies.py / session_verify.py
  services.py / cli.py / mcp_server.py / bootstrap.py / fixtures.py
  adapters/
    base.py / mock.py
    zhs_dom.py           ★ OCS(MIT) 选择器知识，保留版权声明
    zhs_js.py            ★ 注入页面的 JS 载荷
    zhs_browser.py       ★ CDP 通道（读侧 + 答题主力）
    zhs_autovisor.py     ★ Autovisor 子进程 Adapter
    zhs_worker.py        ★ worker 胶水（中文日志 → 机器可读事件）
    manifests/*.json     声明式能力清单
upstreams/               gitignore｜只读 clone，锁 commit
upstreams.lock.json      ★ 锁文件（入库）：commit + 许可证 + 隔离理由
accounts/{id}/           gitignore｜账号工作区（cookie 三格式 / config.ini / 断点）
runs/                    gitignore｜日志 + 答题工单
tests/                   67 项离线测试
```

---

## 复用说明（对应用户原则"不要从零重写"）

统一层里 **22 个平台无关模块**整体复用自姊妹项目
[`cx`（学习通）](https://github.com/changgeng1009/cx)：models / errors / capabilities /
registry / router / state / throttle / risk / structured_log / redact / session /
control / answer_broker / openai_shim / services / cli / mcp_server / bootstrap /
browser / cdp / cookies / session_verify / fixtures。

改造**仅限于**四处：Adapter 注册表、Cookie 域名后缀、风控登录特征、CLI/MCP 文案。

**本项目真正新写的只有 5 个文件**：`safety.py` + 4 个 `zhs_*`。
能力编号（C01–C51）、Envelope 字段、8 态状态机、8 类错误分类、命令名
与两个姊妹项目**逐字一致**——上层 Agent 的调用代码跨平台不用改。

---

## 合规提示

上游项目均声明"仅用于学习交流"。使用本工具可能违反智慧树用户协议，
并存在账号被风控的风险。**建议在个人账号上以最小必要频率使用**，
风险由使用者自行承担。请勿用于商业用途。
**正式考试一律手工完成。**
