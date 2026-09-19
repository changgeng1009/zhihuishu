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

    /** 扫描任务点。返回结构化数组，不做任何点击。 */
    scanTasks() {
      const p = page();
      if (!p) return { ok: false, reason: "unknown_page", items: [] };
      const items = qa(p.itemSelector).map((el, idx) => {
        const finished = p.finishedSelector ? !!el.querySelector(p.finishedSelector) : false;
        const current = p.currentClass ? el.classList.contains(p.currentClass) : false;
        // WISDOM_* / NEW_SHARED：任务点可能嵌在二级容器里
        const nested = el.querySelector(".chapter-content-second");
        return {
          index: idx,
          title: text(el).slice(0, 200),
          finished: finished,
          current: current,
          nested: !!nested,
          // 已完成的条目在 DOM 里通常没有可点节点，用 class 保留原始线索
          className: el.className,
        };
      });
      return { ok: true, page: p.label, count: items.length, items };
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

    /** 切换倍速（平台上限见 CFG.maxSpeed）。 */
    async setSpeed(rate) {
      const bar = q(CFG.controls.controls_bar);
      const list = q(CFG.controls.speed_list);
      if (bar) bar.style.display = "block";
      if (list) list.style.display = "block";
      const parsed = parseFloat(String(rate));
      const v1 = parsed === 1 ? "1.0" : String(parsed);
      const v2 = parsed === 1 ? "1" : String(parsed);
      const btn = q(`.speedList [rate="${v1}"],.speedList [rate="${v2}"]`);
      if (!btn) return { ok: false, reason: "speed_button_not_found", rate: parsed };
      btn.click();
      return { ok: true, rate: parsed };
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

#: 打桩的 `__ZHS_CFG__` 形状（供文档与测试参考，实际由 zhs_browser 生成）
CFG_SHAPE = {
    "currentPage": "<ZhsPage 值>",
    "profiles": "<{pageKey: PageProfile}>",
    "popups": "<{pageKey: PopupProfile}>",
    "controls": "<zhs_dom.CONTROLS>",
    "noiseDialogSelectors": "<zhs_dom.NOISE_DIALOGS>",
    "maxSpeed": "<zhs_dom.MAX_SPEED>",
}
