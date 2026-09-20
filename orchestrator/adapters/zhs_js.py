"""注入到智慧树页面里的 JS 载荷（浏览器通道的执行体）。

## 为什么要注入而不是"用 Playwright 选择器点"

智慧树的学习页是 Vue/Element-Plus 单页应用，任务点、弹题按钮都在**组件内部**，
同一个选择器可能匹配到隐藏节点（"学前必读"弹窗就是隐藏节点——
Autovisor 3.18.3 的更新日志专门修过这个坑）。

因此本模块的 JS 一律遵循两条纪律：
1. **可见性优先**：所有取值都先过滤 `getClientRects().length > 0` 且非 `display:none`。
2. **只回数据不动作**：读取函数返回结构化 JSON；动作函数（点击）单独调用，
   两者分开使"读到的"与"点到的"可以分别审计。

选择器**不在 JS 里硬编码**，而是由 Python 侧（`zhs_dom.py` 的单一事实来源）
在注入前以 `window.__ZHS_CFG__` 传入。这样上游改版时只改一处。

## 与安全守卫的配合

`visibleText()` 是 `safety.verify()` 第二层（DOM 复核）的数据来源。
写操作前必须调用它并把结果回传给 Python 侧裁决。
`answerPopup()` 本身不做任何安全判断——**判断在 Python 侧**，
这样即使 JS 被误调用，也过不了 URL/DOM 两层守卫。
"""

from __future__ import annotations

#: 注入到页面的配置与工具函数。挂到 `window.__ZHS__`。
BOOTSTRAP_JS = r"""
(() => {
  if (window.__ZHS__ && window.__ZHS__.version === "1.0") return;
  const CFG = window.__ZHS_CFG__ || {};

  const visible = (el) => {
    if (!el) return false;
    if (el.getClientRects().length === 0) return false;
    const st = getComputedStyle(el);
    return st.visibility !== "hidden" && st.display !== "none" && st.opacity !== "0";
  };

  const q = (sel, root) => {
    if (!sel) return null;
    try {
      const found = (root || document).querySelector(sel);
      return found && visible(found) ? found : null;
    } catch (e) { return null; }
  };

  const qa = (sel, root) => {
    if (!sel) return [];
    try {
      return Array.from((root || document).querySelectorAll(sel)).filter(visible);
    } catch (e) { return []; }
  };

  const text = (el) => (el && el.textContent ? el.textContent.replace(/\s+/g, " ").trim() : "");

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const page = () => CFG.profiles ? CFG.profiles[CFG.currentPage] : null;

  /** 条目是否是**真正的任务点**（而不是章标题）。
   *
   *  2026-09-20 实测：`studywisdomh5` 课程页里 `.child-main` 命中 104 个，
   *  其中一部分是章标题（"上古至秦萌芽时期"），只有父级带 `.child-time`
   *  （视频时长）的才是任务点。这个约束来自 CFG，不硬编码。 */
  const isTaskItem = (el, p) => {
    if (!p || !p.itemConstraintSelector) return true;
    const sel = p.itemConstraintSelector;
    if (el.querySelector(sel)) return true;
    return !!(el.parentElement && el.parentElement.querySelector(sel));
  };

  const itemsOf = (p) => p ? qa(p.itemSelector).filter((el) => isTaskItem(el, p)) : [];

  /** 按 DOM 命中数自动识别布局。
   *
   *  为什么不能按域名映射：上游 OCS 的 `zhs.ts` 是"域名 → 处理器类"的硬映射，
   *  但实测 `studywisdomh5.zhihuishu.com` 返回的却是 `.child-main` 结构。
   *  平台会把同一种布局挂到不同域名下，也会在同一域名下换布局。
   *  **DOM 事实比域名可靠**，所以以命中数最多者为胜。 */
  const detectLayout = () => {
    const profiles = CFG.profiles || {};
    let best = null;
    const scores = [];
    for (const [key, p] of Object.entries(profiles)) {
      const n = itemsOf(p).length;
      scores.push({ key, label: p.label, count: n });
      if (n > 0 && (!best || n > best.count)) best = { key, label: p.label, count: n };
    }
    scores.sort((a, b) => b.count - a.count);
    return { best, scores };
  };

  const api = {
    version: "1.0",

    /** 页面基本信息；host 与 profileKey 供 Python 侧二次确认路由。 */
    detect() {
      return {
        href: location.href,
        host: location.hostname,
        title: document.title,
        profileKey: CFG.currentPage || null,
        profileLabel: page() ? page().label : null,
      };
    },

    /** 可见文本 —— 考试守卫第二层的数据来源。 */
    visibleText() {
      const body = document.body;
      if (!body) return "";
      return (body.innerText || "").replace(/\s+/g, " ").trim().slice(0, 20000);
    },

    /** 当前 DOM 上命中的任务点数（布局自动识别后）。
     *  给 Python 侧做"等页面渲染完再扫"的轮询条件用 —— 比 sleep 固定秒数稳。 */
    layoutCount() {
      const d = detectLayout();
      return d.best ? d.best.count : 0;
    },

    /** 扫描任务点。返回结构化数组，不做任何点击。 */
    scanTasks() {
      const detected = detectLayout();
      if (!detected.best) {
        return {
          ok: false,
          reason: "no_layout_matched",
          items: [],
          scores: detected.scores,
          href: location.href,
        };
      }
      const p = (CFG.profiles || {})[detected.best.key];
      const all = qa(p.itemSelector);
      const items = [];
      let chapter = "";

      all.forEach((el) => {
        const isTask = isTaskItem(el, p);
        if (!isTask) {
          // 不是任务点 → 当作章标题，后续任务点都归属它
          const t = text(el);
          if (t) chapter = t.slice(0, 120);
          return;
        }
        const parent = el.parentElement || el;
        const parentText = text(parent);
        const icon = p.finishedSelector ? parent.querySelector(p.finishedSelector) : null;
        const timeEl = p.progressSelector ? parent.querySelector(p.progressSelector) : null;
        const duration = timeEl ? text(timeEl) : "";

        // 状态判定，按可靠性从高到低：
        //   1) 完成图标 —— 平台自己的标记，最可靠
        //   2) 完成类文案
        //   3) 未完成类文案
        //   4) **有视频时长但没有完成标记** → 判 todo
        //      第 4 条是必要的兜底：实测状态文案比条目列表晚几百毫秒渲染，
        //      "等到有布局"时文案可能还没出现。而对一个带时长的视频任务点来说，
        //      没有完成标记本身就已说明它没完成 —— 这比报 unknown 有意义。
        let status = "unknown";
        let statusSource = "none";
        if (icon || /已完成|已学完/.test(parentText)) {
          status = "done";
          statusSource = icon ? "finish_icon" : "text";
        } else if (/未练习|未完成|待学习|未开始|进行中|继续学习/.test(parentText)) {
          status = "todo";
          statusSource = "text";
        } else if (duration) {
          status = "todo";
          statusSource = "has_duration_no_finish_mark";
        }

        items.push({
          index: items.length,
          title: text(el).slice(0, 200),
          chapter: chapter,
          finished: status === "done",
          status: status,
          statusSource: statusSource,
          current: p.currentClass ? parent.classList.contains(p.currentClass) : false,
          duration: duration,
          parentClass: parent.className,
        });
      });

      return {
        ok: true,
        page: p.label,
        pageKey: detected.best.key,
        count: items.length,
        rawItemCount: all.length,
        items: items,
        layoutScores: detected.scores,
        dialogues: (CFG.noiseDialogSelectors || []).map((sel) => qa(sel).length),
      };
    },

    /** 读取课中弹题。只读，不点击。 */
    popup() {
      const popups = CFG.popups || {};
      for (const [key, pp] of Object.entries(popups)) {
        const root = q(pp.root_selector);
        if (!root) continue;
        const done = pp.done_selector ? q(pp.done_selector, root) : null;
        const options = qa(pp.option_selector, root).map((el, i) => ({
          index: i,
          key: String.fromCharCode(65 + i),
          text: text(el).slice(0, 500),
          tag: el.tagName.toLowerCase(),
        }));
        const stemEl = pp.stem_selector ? q(pp.stem_selector, root) : null;
        return {
          present: true,
          pageKey: key,
          alreadyDone: !!done,
          stem: stemEl ? text(stemEl).slice(0, 2000) : text(root).slice(0, 2000),
          options: options,
          optionCount: options.length,
        };
      }
      return { present: false };
    },

    /** 关闭弹题弹窗（已作答或已被平台判过之后调用）。 */
    async closePopup() {
      const popups = CFG.popups || {};
      for (const [key, pp] of Object.entries(popups)) {
        const root = q(pp.root_selector);
        if (!root) continue;
        const btn = q(pp.close_selector, root);
        if (btn) { btn.click(); await sleep(600); return { ok: true, pageKey: key }; }
        return { ok: false, reason: "close_button_not_found", pageKey: key };
      }
      return { ok: false, reason: "no_popup" };
    },

    /**
     * 按 Agent 给出的选项下标作答。
     * indices 为空 → 什么都不点（由 Python 侧决定是否转人工），绝不随机作答。
     */
    async answerPopup(indices) {
      const idx = Array.isArray(indices) ? indices : [];
      if (idx.length === 0) {
        return { ok: false, reason: "no_answer_provided" };
      }
      const popups = CFG.popups || {};
      for (const [key, pp] of Object.entries(popups)) {
        const root = q(pp.root_selector);
        if (!root) continue;
        const options = qa(pp.option_selector, root);
        if (options.length === 0) return { ok: false, reason: "no_options", pageKey: key };
        const clicked = [];
        for (const i of idx) {
          if (i >= 0 && i < options.length) {
            options[i].click();
            clicked.push(i);
            await sleep(400);
          }
        }
        if (clicked.length === 0) return { ok: false, reason: "index_out_of_range", pageKey: key };
        await sleep(800);
        const submit = q(pp.submit_selector, root);
        if (submit) { submit.click(); await sleep(1200); }
        return { ok: true, pageKey: key, clicked, submitted: !!submit };
      }
      return { ok: false, reason: "no_popup" };
    },

    /** 关闭遮挡任务流的通知弹窗（不碰弹题）。 */
    closeNoiseDialogs() {
      let n = 0;
      (CFG.noiseDialogSelectors || []).forEach((sel) => {
        qa(sel).forEach((el) => {
          // 只隐藏，不删除：删除可能破坏 Vue 的虚拟 DOM 引用
          el.style.display = "none";
          n += 1;
        });
      });
      return { hidden: n };
    },

    /** 切换倍速。
     *
     *  2026-09-20 实测（真实课程页）：
     *  - 档位由**页面提供**，实测只有 `1.0 / 1.25 / 1.5`（1.5 是最高档），
     *    所以不能假设 1.8 一定存在 —— 请求不到就退到"不超过请求值的最高档"。
     *  - `.speedList` 默认 `display:none`（悬停才展开），必须先展开再点，否则点空。
     *  - **点完必须验** `video.playbackRate`：点击成功 ≠ 倍速生效，
     *    这是唯一能证明"真的 1.5 倍"的判据。
     */
    async setSpeed(rate) {
      const wanted = parseFloat(String(rate));
      if (!isFinite(wanted)) return { ok: false, reason: "invalid_rate", requested: rate };

      const bar = q(CFG.controls.controls_bar);
      const list = q(CFG.controls.speed_list);
      if (bar) bar.style.display = "block";
      if (list) list.style.display = "block";

      // 枚举页面真实提供的档位
      const opts = qa("[rate]").filter((el) => CFG.controls.speed_list
        ? el.closest(CFG.controls.speed_list)
        : true);
      const rates = opts
        .map((el) => ({ el: el, rate: parseFloat(el.getAttribute("rate")) }))
        .filter((o) => isFinite(o.rate));
      if (rates.length === 0) {
        return { ok: false, reason: "no_rate_options", requested: wanted };
      }

      const exact = rates.find((o) => Math.abs(o.rate - wanted) < 0.001);
      const notAbove = rates.filter((o) => o.rate <= wanted + 0.001);
      const pick = exact || notAbove.sort((a, b) => b.rate - a.rate)[0] ||
        rates.sort((a, b) => a.rate - b.rate)[0];

      pick.el.click();
      await sleep(700);

      // 验证：以 video.playbackRate 为准
      const v = document.querySelector("video");
      const actual = v ? v.playbackRate : null;
      const applied = actual !== null && Math.abs(actual - pick.rate) < 0.01;
      return {
        ok: applied,
        requested: wanted,
        selected: pick.rate,
        actualPlaybackRate: actual,
        available: rates.map((o) => o.rate),
        downgraded: Math.abs(pick.rate - wanted) > 0.001,
        reason: applied ? "" : (actual === null ? "no_video_element" : "rate_not_applied"),
      };
    },

    /** 读取视频播放状态（进度检测的浏览器侧数据源）。 */
    videoState() {
      const v = document.querySelector("video");
      if (!v) return { present: false };
      return {
        present: true,
        currentTime: v.currentTime,
        duration: v.duration,
        paused: v.paused,
        ended: v.ended,
        playbackRate: v.playbackRate,
        readyState: v.readyState,
      };
    },

    /** 切到下一个任务点。返回是否成功点击（不做"是否已完成"的判断）。 */
    async goNextTask() {
      const p = page();
      if (!p) return { ok: false, reason: "unknown_page" };
      const items = qa(p.itemSelector);
      let target = null;
      for (let i = 0; i < items.length; i++) {
        if (p.currentClass && items[i].classList.contains(p.currentClass)) {
          target = items[i + 1] || null;
          break;
        }
      }
      if (!target) target = items[0] || null;
      if (!target) return { ok: false, reason: "no_task_item" };
      const clickable = target.querySelector("a,span,div") || target;
      clickable.click();
      return { ok: true, title: text(target).slice(0, 200) };
    },
  };

  window.__ZHS__ = api;
})();
"""

#: 便捷包装：在 CDP 里求值一个返回 Promise 的表达式。
AWAIT_TEMPLATE = "(async () => { %s })()"


# ---------------------------------------------------------------------------
# 接口响应收集器（读侧的数据来源）
# ---------------------------------------------------------------------------
#: 在**页面加载前**注入（`Page.addScriptToEvaluateOnNewDocument`），
#: 记录智慧树自己的 XHR 响应，挂到 `window.__ZHS_API__`。
#:
#: ## 为什么不自己拼请求
#:
#: 2026-09-20 实测：智慧树所有业务接口都带 **`secretStr` 签名参数**
#: （GET 在 query 上、POST 在 body 里），由页面 JS 生成。
#: 逆向它的加密算法既脆弱（改版即失效）又没必要 ——
#: **让页面自己发请求，我们只读它的响应**，天然带齐 cookie 与签名。
#:
#: ## 为什么不做成"页面已加载完再挂钩子"
#:
#: 那样会漏掉首屏那批请求（正是我们要的课程列表）。
#: 所以必须用 `Page.addScriptToEvaluateOnNewDocument` 在文档开始前注入。
#:
#: ## 安全
#:
#: 本脚本**只读**：包装 fetch/XHR 记录响应，不改请求、不改响应、不点击。
#: 记录里显式排除了 `/doExam`、`doHomework`、`examloop` 等考试相关路径 ——
#: 考试红线（`safety.py`）在统一层拦截写操作，这里再加一道：**连数据都不采集**。
API_RECORDER_JS = r"""
(() => {
  if (window.__ZHS_API__) return;

  // 只采集白名单路径；考试/作业提交类一律不碰
  const WANT = [
    "getCourseList", "getNoConfirmCourseList", "queryStudentAICourseList",
    "queryShareCourseInfo", "queryMicroCourseInfo", "queryStudentSchoolCourseList",
    "querySelectCourseInfo", "getCourseDetail", "getStudentCourseDetail",
    "getChapterList", "getCourseChapter", "queryChapter",
    "getCoursePoint", "getJobList", "getStudentJobList",
  ];
  const BLOCK = ["doExam", "doHomework", "examloop", "submithomework", "submitexam"];
  const path = (u) => String(u || "").split("?")[0].split("/").pop();
  const wanted = (u) => {
    const s = String(u || "").toLowerCase();
    if (BLOCK.some((b) => s.includes(b.toLowerCase()))) return false;
    return WANT.some((k) => String(u || "").includes(k));
  };

  const records = [];
  const store = (kind, method, url, body, status, data) => {
    try {
      records.push({
        kind, method: String(method || "GET").toUpperCase(),
        endpoint: path(url), url: String(url || ""),
        status: status, ts: Date.now(),
        data: data === undefined ? null : data,
      });
    } catch (e) {}
  };
  const parse = (t) => { try { return JSON.parse(t); } catch (e) { return null; } };

  const of = window.fetch;
  if (of) {
    window.fetch = function (input, init) {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      const p = of.apply(this, arguments);
      if (!wanted(url)) return p;
      return p.then((resp) => {
        try {
          resp.clone().text().then((t) =>
            store("fetch", (init && init.method) || "GET", url, init && init.body, resp.status, parse(t)));
        } catch (e) {}
        return resp;
      });
    };
  }

  const oo = XMLHttpRequest.prototype.open;
  const os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__m = m; this.__u = u; return oo.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function (b) {
    const x = this;
    if (wanted(x.__u)) {
      x.addEventListener("load", () => {
        let d = null;
        try { d = parse(x.responseText); } catch (e) {}
        store("xhr", x.__m, x.__u, b, x.status, d);
      });
    }
    return os.apply(this, arguments);
  };

  window.__ZHS_API__ = {
    version: "1.0",
    all: () => records.slice(),
    byEndpoint: (name) => records.filter((r) => r.endpoint === name),
    latest: (name) => {
      const hits = records.filter((r) => r.endpoint === name);
      return hits.length ? hits[hits.length - 1] : null;
    },
    count: () => records.length,
  };
})();
"""

#: 智慧树「我的课程」的三个数据源与各自的解析路径。
#:
#: 2026-09-20 实测（账号真实数据）：
#: - `queryShareCourseInfo`      POST → `result.courseOpenDtos[]`  共享课（4 门）
#: - `queryStudentAICourseList`  POST → `rt[]`                     AI 课（1 门）
#: - `getCourseList`             GET  → `rt.pageList[]`            新系统课（本账号为空）
#:
#: 三条都是"可缺"的：任一为空不代表失败，只代表该类型没课。
COURSE_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("queryShareCourseInfo", "result.courseOpenDtos", "shared"),
    ("queryStudentAICourseList", "rt", "ai"),
    ("getCourseList", "rt.pageList", "new_system"),
    ("queryMicroCourseInfo", "result.courseOpenDtos", "micro"),
    ("querySelectCourseInfo", "result", "select"),
)

#: 读取并归一化课程列表（在页面里执行）。
#:
#: 归一化在**页面侧**做，只把结果回传：一是数据量小，二是不用把
#: 平台的原始结构整包搬进 Python 再解析。
NORMALIZE_SOURCES_JS = r"""
(() => {
  const API = window.__ZHS_API__;
  if (!API) return { ok: false, reason: "recorder_not_installed", courses: [] };

  const dig = (obj, path) => {
    let cur = obj;
    for (const part of path.split(".")) {
      if (cur === null || cur === undefined) return undefined;
      cur = cur[part];
    }
    return cur;
  };
  const text = (v) => (v === null || v === undefined ? "" : String(v).replace(/\s+/g, " ").trim());
  const num = (v) => { const n = parseFloat(String(v ?? "").replace(/[^\d.-]/g, "")); return isNaN(n) ? null : n; };

  const courses = [];
  const seen = new Set();
  const sources = [];

  for (const [endpoint, path, kind] of (window.__ZHS_CFG__ || {}).courseSources || []) {
    const rec = API.latest(endpoint);
    const raw = rec ? dig(rec.data, path) : undefined;
    const list = Array.isArray(raw) ? raw : [];
    sources.push({ endpoint, kind, found: rec ? list.length : 0, seen: !!rec });
    for (const item of list) {
      if (!item || typeof item !== "object") continue;
      // 智慧树课程 id 有两种口径：共享课用 courseId（数字），AI 课用 courseId（雪花）
      const courseId = text(item.courseId ?? item.course_id ?? "");
      if (!courseId) continue;
      const key = kind + ":" + courseId;
      if (seen.has(key)) continue;
      seen.add(key);
      // 学习页地址。2026-09-20 实测从首页 DOM 反推出来的：
      //   共享课  https://studywisdomh5.zhihuishu.com/study/index?recruitAndCourseId=<secret>
      //          —— `secret` 就是 queryShareCourseInfo 返回的那个字段，不是拼出来的
      //   AI 课   linkUrl 字段直接给全（ai-smart-course-student-pro.zhihuishu.com/...）
      // 这两条是"平台自己用的地址"，比我们猜参数名可靠得多。
      const learnUrl = kind === "ai"
        ? text(item.linkUrl ?? "")
        : (item.secret
            ? "https://studywisdomh5.zhihuishu.com/study/index?recruitAndCourseId=" + text(item.secret)
            : "");
      courses.push({
        course_id: courseId,
        learn_url: learnUrl,
        secret: text(item.secret ?? ""),
        recruit_id: text(item.recruitId ?? item.recruit_id ?? ""),
        clazz_id: text(item.classId ?? item.class_id ?? ""),
        cpi: "",
        name: text(item.courseName ?? item.course_name ?? ""),
        teacher: text(item.teacherName ?? item.teacherUserName ?? item.teacher_name ?? ""),
        school: text(item.schoolName ?? item.school_name ?? ""),
        fid: text(item.schoolId ?? item.school_id ?? ""),
        progress_text: text(item.progress ?? item.mastery ?? ""),
        progress_percent: num(item.progress ?? item.mastery),
        term: text(item.termName ?? item.term_name ?? ""),
        course_kind: kind,
        course_type: text(item.courseType ?? item.type ?? ""),
        class_name: text(item.className ?? item.class_name ?? ""),
      });
    }
  }

  return {
    ok: true,
    href: location.href,
    total: courses.length,
    courses: courses,
    sources: sources,
    recorded: API.count(),
  };
})()
"""

#: 注入的 `__ZHS_CFG__` 形状（供文档与测试参考，实际由 build_page_config 生成）
CFG_SHAPE = {
    "currentPage": "<ZhsPage 值>",
    "profiles": "<{pageKey: PageProfile 的字段子集}>",
    "popups": "<{pageKey: PopupProfile}>",
    "controls": "<zhs_dom.CONTROLS>",
    "noiseDialogSelectors": "<zhs_dom.NOISE_DIALOGS>",
    "maxSpeed": "<zhs_dom.MAX_SPEED>",
    "courseSources": "<[(endpoint, 数据路径, 类型)]，见 COURSE_SOURCES>",
}
