/* 签到控制中心前端：Vanilla JS + 现代交互 + 动态表单 + SSE 实时流 */
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

let sitesMeta = [];
let allAccounts = [];
let activeEventSource = null;
let activeRunId = null;
let editingId = null;
let historyPage = 1;
const historyPageSize = 10;
let historyTotal = 0;
let lastLiveSteps = [];
let watchSource = null;

/* ---------- 站点分页状态 ---------- */
let sitePages = [];        // 按 sitesMeta 顺序、有账号的 siteKey 列表（已应用搜索过滤）
let siteIdx = 0;           // 当前站点页下标
let acctPageBySite = {};   // siteKey -> 账号内页码（从 1 开始）
const ACCT_PAGE_SIZE = 5;

function currentSiteKey() {
  if (!sitePages.length) return null;
  return sitePages[Math.min(siteIdx, sitePages.length - 1)];
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 2800);
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (r.status === 401) {
    const data = await r.json().catch(() => ({}));
    if (data.authRequired) {
      showAuthModal();
      throw new Error("请先输入密码登录");
    }
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || data.error || `请求失败 (${r.status})`);
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function fmtNext(iso) {
  if (!iso) return "自动签到未开启";
  const d = new Date(iso);
  const p = (n) => String(n).padStart(2, "0");
  return `下次运行：${d.getMonth() + 1}月${d.getDate()}日 ${p(d.getHours())}:${p(d.getMinutes())}（北京时间）`;
}

function fmtTs(ts) {
  if (!ts) return "";
  const m = ts.match(/T(\d{2}:\d{2}:\d{2})/);
  if (m) {
    const d = ts.slice(0, 10);
    return `${d.slice(5)} ${m[1]}`;
  }
  return ts;
}

function calcDuration(startIso, endIso) {
  if (!startIso || !endIso) return "";
  const start = new Date(startIso).getTime();
  const end = new Date(endIso).getTime();
  const diffSec = Math.max(0, (end - start) / 1000);
  return `耗时 ${diffSec.toFixed(1)}s`;
}

function debounce(fn, delay = 200) {
  let timer = null;
  return function (...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), delay);
  };
}

/* ---------- 主题切换 ---------- */
function initTheme() {
  const saved = localStorage.getItem("checkin_theme");
  if (saved === "dark" || (!saved && window.matchMedia("(prefers-color-scheme: dark)").matches)) {
    document.body.classList.add("dark-mode");
    $("#theme-icon").textContent = "☀️";
  } else {
    document.body.classList.remove("dark-mode");
    $("#theme-icon").textContent = "🌙";
  }
}
function toggleTheme() {
  const isDark = document.body.classList.toggle("dark-mode");
  localStorage.setItem("checkin_theme", isDark ? "dark" : "light");
  $("#theme-icon").textContent = isDark ? "☀️" : "🌙";
}

/* ---------- 鉴权控制 ---------- */
async function checkAuth() {
  try {
    const res = await api("/api/auth/status");
    if (res.authEnabled) {
      $("#btn-logout").style.display = "";
      if (!res.authenticated) {
        showAuthModal();
        return false;
      }
    }
    return true;
  } catch (e) {
    return false;
  }
}
function showAuthModal() {
  $("#auth-modal").classList.add("show");
  $("#auth-password").focus();
}
function hideAuthModal() {
  $("#auth-modal").classList.remove("show");
}

/* ---------- 站点元数据与动态表单 ---------- */
async function loadSites() {
  try {
    const data = await api("/api/sites");
    sitesMeta = data.sites || [];

    // 渲染表单站点下拉菜单
    const formSite = $("#f-site");
    formSite.innerHTML = sitesMeta.map((s) => `<option value="${esc(s.key)}">${esc(s.name)}</option>`).join("");
  } catch (e) {
    console.error("加载站点元数据失败", e);
  }
}

function renderDynamicFields(siteKey, isEdit = false) {
  const site = sitesMeta.find((s) => s.key === siteKey);
  const container = $("#dynamic-fields");
  if (!site || !site.fields) {
    container.innerHTML = "";
    return;
  }
  container.innerHTML = site.fields
    .map((f) => {
      const ph = isEdit ? "留空表示不更换" : (f.placeholder || "");
      const isPwd = f.type === "password";
      return `
      <div class="field">
        <label>${esc(f.label)} ${f.required && !isEdit ? '<span style="color:var(--red)">*</span>' : ""}</label>
        <div class="input-wrap">
          <input type="${esc(f.type || "text")}" id="dyn-${esc(f.key)}" data-key="${esc(f.key)}" placeholder="${esc(ph)}">
          ${isPwd ? `<button type="button" class="btn-toggle-pwd" data-target="dyn-${esc(f.key)}" title="切换明文/密文">👁️</button>` : ""}
        </div>
        ${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ""}
      </div>`;
    })
    .join("");

  // 绑定密码眼睛显隐切换
  container.querySelectorAll(".btn-toggle-pwd").forEach((btn) => {
    btn.addEventListener("click", () => {
      const targetId = btn.dataset.target;
      const inp = document.getElementById(targetId);
      if (inp) {
        const isPassword = inp.type === "password";
        inp.type = isPassword ? "text" : "password";
        btn.textContent = isPassword ? "🙈" : "👁️";
      }
    });
  });
}

/* ---------- 看板统计与账号列表 ---------- */
function renderStats(stats) {
  if (!stats) return;
  $("#stat-total").textContent = stats.totalAccounts;
  $("#stat-enabled-sub").textContent = `已启用 ${stats.enabledAccounts} / 停用 ${stats.totalAccounts - stats.enabledAccounts}`;
  $("#stat-success").textContent = stats.todaySuccess;
  $("#stat-failed").textContent = stats.todayFailed;
  $("#stat-pending").textContent = stats.todayPending;

  const quickRetryBtn = $("#btn-quick-retry");
  if (stats.todayFailed > 0) {
    quickRetryBtn.style.display = "inline-block";
  } else {
    quickRetryBtn.style.display = "none";
  }
}

function todayBadge(t, decryptError) {
  if (decryptError) return `<span class="badge err" title="密钥变更导致凭据无法解密">⚠️ 凭据解密失败</span>`;
  if (!t) return `<span class="badge wait">今日未签到</span>`;
  if (t.status === "success") return `<span class="badge ok">今日已签到</span>`;
  if (t.status === "cancelled") return `<span class="badge off">今日已终止</span>`;
  return `<span class="badge err">今日失败</span>`;
}

function matchKeyword(a, keyword) {
  if (!keyword) return true;
  const matchNick = (a.nickname || "").toLowerCase().includes(keyword);
  const matchLabel = (a.accountLabel || "").toLowerCase().includes(keyword);
  const matchSite = (a.siteName || "").toLowerCase().includes(keyword);
  return matchNick || matchLabel || matchSite;
}

function accountCard(a) {
  const t = a.today;
  const metric = t && t.metric_label
    ? `<span>${esc(t.metric_label)} <b>${esc(t.metric_value ?? "—")}</b></span>` : "";
  const streak = t && t.streak_days != null
    ? `<span>连签 <b>${t.streak_days}</b> 天</span>` : "";
  return `
  <div class="acct ${a.enabled ? "" : "disabled"}" data-id="${a.id}">
    <div class="row" style="justify-content:space-between">
      <span class="site">${esc(a.siteName)}</span>
      <input type="checkbox" class="pick" data-id="${a.id}" ${a.enabled ? "" : "disabled"} title="勾选后批量操作">
    </div>
    <div class="label" data-act="copy-label" title="点击复制账号标识">
      ${esc(a.nickname || a.accountLabel)}
      <span class="copy-tip">📋</span>
    </div>
    ${a.nickname ? `<div class="nick">${esc(a.accountLabel)}</div>` : ""}
    <div class="today">${todayBadge(t, a.decryptError)}${t && t.conclusion ? ` <span class="muted">${esc(t.conclusion)}</span>` : ""}</div>
    <div class="metrics">${metric}${streak}
      ${a.credentialConfigured ? "" : `<span class="badge err">凭据需配置</span>`}
      ${a.enabled ? "" : `<span class="badge off">已停用</span>`}
    </div>
    <div class="actions">
      <button data-act="single-run" class="btn-single-checkin" ${a.enabled ? "" : "disabled"} title="单独签到该账号">⚡ 签到</button>
      <button data-act="edit">编辑</button>
      <button data-act="toggle">${a.enabled ? "停用" : "启用"}</button>
      <button data-act="del" class="danger">删除</button>
    </div>
  </div>`;
}

function renderSitePager() {
  const box = $("#site-quick-btns");
  box.innerHTML = sitePages
    .map((k, i) => `<button class="site-btn${i === siteIdx ? " active" : ""}" data-site-idx="${i}">${esc(siteName(k))}</button>`)
    .join("");
  box.querySelectorAll(".site-btn").forEach((b) =>
    b.addEventListener("click", () => {
      siteIdx = parseInt(b.dataset.siteIdx, 10) || 0;
      renderAccounts(allAccounts);
    })
  );
  $("#btn-site-prev").disabled = siteIdx <= 0;
  $("#btn-site-next").disabled = siteIdx >= sitePages.length - 1;
  const sk = currentSiteKey();
  $("#site-page-info").textContent = sk
    ? `${siteName(sk)} · 第 ${siteIdx + 1} / ${sitePages.length} 站`
    : "";
}

function renderAccounts(accounts) {
  const box = $("#accounts");
  allAccounts = accounts || [];
  const keyword = ($("#acct-search").value || "").trim().toLowerCase();
  const sortBy = $("#acct-sort").value;

  let filtered = allAccounts.filter((a) => matchKeyword(a, keyword));

  // 排序
  if (sortBy === "status") {
    // 未签/失败在前，已签在后
    filtered.sort((a, b) => {
      const statusWeight = (acct) => {
        if (!acct.today) return 0; // 未签
        if (acct.today.status !== "success") return 1; // 失败
        return 2; // 已签
      };
      return statusWeight(a) - statusWeight(b);
    });
  } else if (sortBy === "streak") {
    filtered.sort((a, b) => {
      const sa = a.today?.streak_days || 0;
      const sb = b.today?.streak_days || 0;
      return sb - sa;
    });
  } else if (sortBy === "site") {
    filtered.sort((a, b) => (a.siteKey || "").localeCompare(b.siteKey || ""));
  }

  // 按站点分组：同站点账号归为同一页，站点顺序跟随 sitesMeta
  const order = sitesMeta.map((s) => s.key);
  const groups = new Map();
  filtered.forEach((a) => {
    if (!groups.has(a.siteKey)) groups.set(a.siteKey, []);
    groups.get(a.siteKey).push(a);
  });
  sitePages = [...groups.keys()].sort((x, y) => {
    const ix = order.indexOf(x), iy = order.indexOf(y);
    return (ix === -1 ? 999 : ix) - (iy === -1 ? 999 : iy);
  });
  if (siteIdx >= sitePages.length) siteIdx = 0;
  renderSitePager();

  const sk = currentSiteKey();
  const pager = $("#acct-pagination");
  if (!sk) {
    box.innerHTML = `<div class="muted" style="grid-column:1/-1; padding:30px; text-align:center">没有找到符合条件的账号。</div>`;
    pager.style.display = "none";
    updateSelCount();
    return;
  }

  // 同一站点内分页：每页最多 5 个账号
  const siteAccts = groups.get(sk);
  const totalPages = Math.max(1, Math.ceil(siteAccts.length / ACCT_PAGE_SIZE));
  let pg = Math.min(Math.max(acctPageBySite[sk] || 1, 1), totalPages);
  acctPageBySite[sk] = pg;
  const pageAccts = siteAccts.slice((pg - 1) * ACCT_PAGE_SIZE, pg * ACCT_PAGE_SIZE);

  box.innerHTML = pageAccts.map(accountCard).join("");

  if (totalPages > 1) {
    pager.style.display = "";
    $("#acct-page-info").textContent = `第 ${pg} / ${totalPages} 页（共 ${siteAccts.length} 个账号）`;
    $("#acct-page-jump").innerHTML = Array.from({ length: totalPages }, (_, i) =>
      `<option value="${i + 1}"${i + 1 === pg ? " selected" : ""}>第 ${i + 1} 页</option>`
    ).join("");
    $("#btn-acct-prev").disabled = pg <= 1;
    $("#btn-acct-next").disabled = pg >= totalPages;
  } else {
    pager.style.display = "none";
  }

  updateSelCount();
}

function updateSelCount() {
  const n = $$(".pick:checked").length;
  $("#sel-count").textContent = n;
  $("#btn-manual").disabled = n === 0;
  $$(".batch-btn").forEach((b) => (b.disabled = n === 0));
}

// 统一使用事件委托处理账号卡片内操作
$("#accounts").addEventListener("click", async (e) => {
  const card = e.target.closest(".acct");
  if (!card) return;
  const id = card.dataset.id;
  const act = e.target.closest("[data-act]")?.dataset.act;

  if (e.target.classList.contains("pick")) {
    updateSelCount();
    return;
  }

  if (act === "copy-label") {
    const acct = allAccounts.find((a) => a.id === id);
    if (acct) {
      navigator.clipboard.writeText(acct.accountLabel || acct.nickname || "");
      toast("已复制账号标识");
    }
  } else if (act === "single-run") {
    try {
      const r = await api("/api/runs/manual", {
        method: "POST", body: JSON.stringify({ accountIds: [id] }),
      });
      toast(r.message);
      followRunSSE(r.runId);
    } catch (err) {
      toast(err.message);
    }
  } else if (act === "edit") {
    const acct = allAccounts.find((a) => a.id === id);
    openModal("编辑账号", acct);
  } else if (act === "toggle") {
    const enabling = card.classList.contains("disabled");
    if (!enabling && !confirm("停用后该账号不再参与自动签到，手动签到时也会置灰。确定停用？")) return;
    await api(`/api/accounts/${id}/toggle`, {
      method: "POST", body: JSON.stringify({ enabled: enabling }),
    });
    toast(enabling ? "账号已启用" : "账号已停用");
    load();
  } else if (act === "del") {
    if (!confirm("删除后该账号的凭据会被彻底删除（历史记录中的账号标签会保留）。确定删除吗？")) return;
    if (!confirm("再次确认：真的要删除这个账号吗？此操作不可撤销。")) return;
    await api(`/api/accounts/${id}`, { method: "DELETE" });
    toast("账号已删除");
    load();
  }
});

/* ---------- 批量操作 ---------- */
async function batchToggleAccounts(enabled) {
  const ids = [...$$(".pick:checked")].map((c) => c.dataset.id);
  if (!ids.length) return;
  const actionText = enabled ? "启用" : "停用";
  if (!confirm(`确定要批量${actionText}选中的 ${ids.length} 个账号吗？`)) return;

  for (const id of ids) {
    try {
      await api(`/api/accounts/${id}/toggle`, {
        method: "POST", body: JSON.stringify({ enabled }),
      });
    } catch (e) {
      console.error(e);
    }
  }
  toast(`已批量${actionText} ${ids.length} 个账号`);
  load();
}

/* ---------- 历史记录 ---------- */
async function loadRuns() {
  const status = $("#history-filter-status").value;
  const offset = (historyPage - 1) * historyPageSize;
  try {
    let url = `/api/runs?limit=${historyPageSize}&offset=${offset}`;
    if (status) url += `&status=${status}`;
    const data = await api(url);
    historyTotal = data.total || 0;
    renderRuns(data.runs || []);
    renderPagination();
  } catch (e) {
    $("#runs").innerHTML = `<div class="muted">${esc(e.message)}</div>`;
  }
}

function renderRuns(runs) {
  const box = $("#runs");
  if (!runs.length) {
    box.innerHTML = `<div class="muted" style="padding:16px; text-align:center">暂无运行记录。</div>`;
    return;
  }
  box.innerHTML = runs
    .map((r) => {
      const mode = r.mode === "scheduled" ? "自动签到" : "手动签到";
      const st = r.status === "running"
        ? `<span class="badge wait">进行中</span>`
        : r.status === "cancelled"
        ? `<span class="badge off">已终止</span>`
        : r.status === "failed"
        ? `<span class="badge err">失败</span>`
        : `<span class="badge ok">已完成</span>`;
      const sum = r.total
        ? `<span class="muted small">${r.succeeded} 成功 / ${r.failed} 失败</span>` : "";
      const duration = calcDuration(r.created_at, r.completed_at);

      return `
      <div class="run" data-id="${r.id}">
        <div class="head">
          <b>${mode}</b>${st}${sum}
          <span class="muted small">${fmtTs(r.created_at)}</span>
          ${duration ? `<span class="muted small">(${esc(duration)})</span>` : ""}
          <span style="flex:1"></span>
          <span class="muted small">▾ 详情</span>
        </div>
        <div class="detail"><div class="spin"></div> 加载中…</div>
      </div>`;
    })
    .join("");

  box.querySelectorAll(".run .head").forEach((h) =>
    h.addEventListener("click", () => toggleRun(h.parentElement))
  );
}

function renderPagination() {
  const totalPages = Math.ceil(historyTotal / historyPageSize) || 1;
  $("#page-info").textContent = `第 ${historyPage} / ${totalPages} 页 (共 ${historyTotal} 条)`;
  $("#btn-prev-page").disabled = historyPage <= 1;
  $("#btn-next-page").disabled = historyPage >= totalPages;
}

async function toggleRun(el) {
  el.classList.toggle("open");
  if (!el.classList.contains("open")) return;
  const id = el.dataset.id;
  const detail = el.querySelector(".detail");
  try {
    const run = await api(`/api/runs/${id}`);
    detail.innerHTML = renderRunDetail(run);
    detail.querySelector(".btn-copy-history-log")?.addEventListener("click", () => {
      copyRunLog(run);
    });
  } catch (e) {
    detail.textContent = e.message;
  }
}

function renderRunDetail(run) {
  const steps = (run.steps || [])
    .map((s) => `<div class="step"><span class="ts">${fmtTs(s.ts)}</span>${esc(s.message)}</div>`)
    .join("");
  const rows = (run.results || [])
    .map((r) => `
      <tr>
        <td>${esc(siteName(r.site_key))}</td>
        <td>${esc(r.account_label)}</td>
        <td>${r.status === "success" ? '<span class="badge ok">成功</span>' : (r.status === "cancelled" ? '<span class="badge off">已终止</span>' : '<span class="badge err">失败</span>')}</td>
        <td>${esc(r.conclusion || "")}</td>
        <td>${esc(r.metric_label || "")} ${esc(r.metric_value ?? "")}</td>
        <td>${r.streak_days ?? "—"}</td>
      </tr>`)
    .join("");
  return `
    <div class="row" style="justify-content:space-between; margin-bottom:8px">
      <span class="muted small">执行详细步骤</span>
      <button class="small-btn btn-copy-history-log">📋 复制运行日志</button>
    </div>
    ${run.error ? `<div class="badge err" style="margin-bottom:8px">${esc(run.error)}</div>` : ""}
    <div class="timeline">${steps || '<div class="muted small">暂无步骤</div>'}</div>
    ${rows ? `<table><tr><th>站点</th><th>账号</th><th>状态</th><th>结论</th><th>余额</th><th>连签</th></tr>${rows}</table>` : ""}`;
}

function copyRunLog(run) {
  const lines = [
    `【签到运行日志】ID: ${run.id}`,
    `模式: ${run.mode === "scheduled" ? "自动定时" : "手动执行"} | 状态: ${run.status}`,
    `时间: ${run.created_at} ~ ${run.completed_at || "运行中"}`,
    "",
    "--- 步骤记录 ---",
  ];
  (run.steps || []).forEach((s) => lines.push(`[${s.ts}] ${s.message}`));
  lines.push("", "--- 账号结果 ---");
  (run.results || []).forEach((r) => {
    lines.push(`[${siteName(r.site_key)}] ${r.account_label}: ${r.status} (${r.conclusion || ""})`);
  });
  navigator.clipboard.writeText(lines.join("\n"));
  toast("运行日志已复制到剪贴板");
}

function siteName(k) {
  const found = sitesMeta.find((s) => s.key === k);
  return found ? found.name : k;
}

/* ---------- 整体页面数据加载 ---------- */
async function load() {
  try {
    const d = await api("/api/dashboard");
    $("#set-enabled").checked = d.autoCheckin.enabled;
    $("#set-time").value = d.autoCheckin.time;
    $("#next-run").textContent = fmtNext(d.autoCheckin.nextRunAt);
    renderStats(d.stats);
    renderAccounts(d.accounts);
    loadRuns();

    const indicator = $("#running-indicator");
    if (d.running) {
      indicator.style.display = "inline-flex";
      if (d.activeRunId) followRunSSE(d.activeRunId);
    } else {
      indicator.style.display = "none";
    }
  } catch (e) {
    toast("加载数据失败：" + e.message);
  }
}

/* ---------- 账号表单操作 ---------- */
function openModal(title, acct) {
  editingId = acct ? acct.id : null;
  $("#modal-title").textContent = title;
  // 新增账号默认归属当前所在的站点页
  const siteKey = acct ? acct.siteKey : (currentSiteKey() || (sitesMeta[0] ? sitesMeta[0].key : "jiaobenwang"));
  $("#f-site").value = siteKey;
  $("#f-site").disabled = !!acct;
  $("#f-nick").value = acct ? (acct.nickname || "") : "";
  $("#f-enabled").checked = acct ? acct.enabled : true;

  renderDynamicFields(siteKey, !!acct);
  $("#modal").classList.add("show");
  $("#f-nick").focus();
}

function closeModal() {
  $("#modal").classList.remove("show");
  editingId = null;
}

/* ---------- watchRuns：订阅新运行启动事件 ---------- */
function watchNewRuns() {
  try {
    if (watchSource) watchSource.close();
    watchSource = new EventSource("/api/runs/watch");
    watchSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        // 收到新 runId（尤其定时任务触发的 scheduled 运行）后自动接入该 run 的实时流；
        // 若已在跟随同一运行则跳过，避免重复订阅
        if (data.type === "run" && data.runId && data.runId !== activeRunId) {
          toast("检测到新的签到运行，正在接入实时进度…");
          followRunSSE(data.runId);
        }
      } catch (err) {
        console.error("解析 watchRuns 消息错误", err);
      }
    };
    // EventSource 断线会自动重连，这里保持静默
    watchSource.onerror = () => {};
  } catch (e) {
    console.warn("watchRuns 订阅失败", e);
  }
}

/* ---------- SSE 实时日志流 ---------- */
function followRunSSE(runId) {
  activeRunId = runId;
  $("#live-card").style.display = "";
  $("#live-timeline").innerHTML = "";
  $("#live-results").innerHTML = "";
  $("#running-indicator").style.display = "inline-flex";
  lastLiveSteps = [];

  if (activeEventSource) {
    activeEventSource.close();
  }

  const tl = $("#live-timeline");
  const seen = new Set();

  try {
    activeEventSource = new EventSource(`/api/runs/${runId}/stream`);
    activeEventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === "step") {
          const s = data.step;
          lastLiveSteps.push(s);
          const key = s.id || (s.ts + s.message);
          if (seen.has(key)) return;
          seen.add(key);
          const div = document.createElement("div");
          div.className = "step";
          div.innerHTML = `<span class="ts">${fmtTs(s.ts)}</span>${esc(s.message)}`;
          tl.appendChild(div);
          tl.scrollTop = tl.scrollHeight;
        } else if (data.type === "finish") {
          activeEventSource.close();
          activeEventSource = null;
          activeRunId = null;
          $("#running-indicator").style.display = "none";
          const run = data.run;
          $("#live-results").innerHTML = renderRunDetail(run);
          const statusText = run.status === "cancelled" ? "已终止" : `${run.succeeded} 成功 / ${run.failed} 失败`;
          toast(`签到结束：${statusText}`);
          setTimeout(load, 1000);
        }
      } catch (err) {
        console.error("解析 SSE 消息错误", err);
      }
    };
    activeEventSource.onerror = () => {
      if (activeEventSource) {
        activeEventSource.close();
        activeEventSource = null;
      }
    };
  } catch (e) {
    console.warn("浏览器不支持 SSE 或连接失败", e);
  }
}

/* ---------- 终止运行 ---------- */
async function cancelActiveRun() {
  if (!activeRunId) return;
  if (!confirm("确定要终止正在运行的签到任务吗？")) return;
  try {
    const res = await api(`/api/runs/${activeRunId}/cancel`, { method: "POST" });
    toast(res.message);
  } catch (e) {
    toast("终止失败：" + e.message);
  }
}

/* ---------- 保存账号 ---------- */
async function saveAccount() {
  const siteKey = $("#f-site").value;
  const nickname = $("#f-nick").value.trim() || null;
  const enabled = $("#f-enabled").checked;
  const site = sitesMeta.find((s) => s.key === siteKey);

  const payload = {
    siteKey,
    nickname,
    enabled,
  };

  let hasFilledCredential = false;
  if (site && site.fields) {
    for (const f of site.fields) {
      const input = $(`#dyn-${f.key}`);
      const val = input ? input.value.trim() : "";
      if (val) hasFilledCredential = true;
      payload[f.key] = val || null;
    }
  }

  try {
    if (editingId) {
      await api(`/api/accounts/${editingId}`, {
        method: "PUT",
        body: JSON.stringify({
          nickname: payload.nickname,
          enabled: payload.enabled,
          replaceCredentials: hasFilledCredential,
          login: payload.login,
          password: payload.password,
          apiKey: payload.apiKey,
        }),
      });
      toast("账号已更新");
    } else {
      await api("/api/accounts", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      toast("账号已添加");
    }
    closeModal();
    load();
  } catch (e) {
    toast(e.message);
  }
}

/* ---------- 事件绑定 ---------- */
$("#btn-theme").addEventListener("click", toggleTheme);
$("#btn-refresh").addEventListener("click", load);
$("#btn-export").addEventListener("click", () => {
  window.open("/api/export/today.csv", "_blank");
});
$("#btn-cancel-run").addEventListener("click", cancelActiveRun);
$("#btn-copy-live-log").addEventListener("click", () => {
  if (!lastLiveSteps.length) return toast("暂无实时日志");
  const text = lastLiveSteps.map((s) => `[${s.ts}] ${s.message}`).join("\n");
  navigator.clipboard.writeText(text);
  toast("实时日志已复制");
});

$("#btn-quick-retry").addEventListener("click", async () => {
  try {
    const r = await api("/api/runs/retry-failed", { method: "POST" });
    toast(r.message);
    followRunSSE(r.runId);
  } catch (e) { toast(e.message); }
});

$("#btn-save-settings").addEventListener("click", async () => {
  try {
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        enabled: $("#set-enabled").checked,
        time: $("#set-time").value,
      }),
    });
    toast("自动签到设置已保存");
    load();
  } catch (e) { toast(e.message); }
});

$("#btn-run-now").addEventListener("click", async () => {
  if (!confirm("立即对全部已启用账号执行一次自动签到？")) return;
  try {
    const r = await api("/api/runs/now", { method: "POST" });
    toast(r.message);
    followRunSSE(r.runId);
  } catch (e) { toast(e.message); }
});

$("#btn-manual").addEventListener("click", async () => {
  const ids = [...$$(".pick:checked")].map((c) => c.dataset.id);
  try {
    const r = await api("/api/runs/manual", {
      method: "POST", body: JSON.stringify({ accountIds: ids }),
    });
    toast(r.message);
    followRunSSE(r.runId);
  } catch (e) { toast(e.message); }
});

$("#btn-select-all").addEventListener("click", () => {
  $$(".pick:not(:disabled)").forEach((c) => (c.checked = true));
  updateSelCount();
});
$("#btn-deselect-all").addEventListener("click", () => {
  $$(".pick").forEach((c) => (c.checked = false));
  updateSelCount();
});

$("#btn-batch-enable").addEventListener("click", () => batchToggleAccounts(true));
$("#btn-batch-disable").addEventListener("click", () => batchToggleAccounts(false));

$("#acct-search").addEventListener("input", debounce(() => renderAccounts(allAccounts), 150));
$("#acct-sort").addEventListener("change", () => renderAccounts(allAccounts));

/* ---------- 站点分页与账号内分页 ---------- */
$("#btn-site-prev").addEventListener("click", () => {
  if (siteIdx > 0) {
    siteIdx--;
    renderAccounts(allAccounts);
  }
});
$("#btn-site-next").addEventListener("click", () => {
  if (siteIdx < sitePages.length - 1) {
    siteIdx++;
    renderAccounts(allAccounts);
  }
});
$("#btn-acct-prev").addEventListener("click", () => {
  const sk = currentSiteKey();
  if (sk && (acctPageBySite[sk] || 1) > 1) {
    acctPageBySite[sk]--;
    renderAccounts(allAccounts);
  }
});
$("#btn-acct-next").addEventListener("click", () => {
  const sk = currentSiteKey();
  if (sk) {
    acctPageBySite[sk] = (acctPageBySite[sk] || 1) + 1;
    renderAccounts(allAccounts);
  }
});
$("#acct-page-jump").addEventListener("change", (e) => {
  const sk = currentSiteKey();
  if (sk) {
    acctPageBySite[sk] = parseInt(e.target.value, 10) || 1;
    renderAccounts(allAccounts);
  }
});

$("#history-filter-status").addEventListener("change", () => {
  historyPage = 1;
  loadRuns();
});
$("#btn-prev-page").addEventListener("click", () => {
  if (historyPage > 1) {
    historyPage--;
    loadRuns();
  }
});
$("#btn-next-page").addEventListener("click", () => {
  historyPage++;
  loadRuns();
});

$("#btn-add").addEventListener("click", () => openModal("添加账号", null));
$("#btn-cancel").addEventListener("click", closeModal);
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
$("#f-site").addEventListener("change", (e) => renderDynamicFields(e.target.value, false));
$("#btn-save-acct").addEventListener("click", saveAccount);

/* ---------- 快捷键支持 ---------- */
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if ($("#modal").classList.contains("show")) closeModal();
  } else if (e.key === "Enter" && $("#modal").classList.contains("show")) {
    if (e.target.tagName !== "BUTTON") {
      saveAccount();
    }
  } else if (e.key === "/" && document.activeElement.tagName !== "INPUT" && !$("#modal").classList.contains("show")) {
    e.preventDefault();
    $("#acct-search").focus();
  } else if (e.key === "r" && document.activeElement.tagName !== "INPUT" && !$("#modal").classList.contains("show")) {
    load();
  }
});

/* ---------- 登录鉴权 ---------- */
$("#btn-auth-login").addEventListener("click", async () => {
  const pwd = $("#auth-password").value;
  if (!pwd) return toast("请输入密码");
  try {
    await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ password: pwd }),
    });
    hideAuthModal();
    toast("登录成功");
    load();
  } catch (e) {
    toast(e.message);
  }
});

$("#btn-logout").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
    toast("已退出登录");
    showAuthModal();
  } catch (e) {
    toast(e.message);
  }
});

/* ---------- 初始化 ---------- */
(async function init() {
  initTheme();
  const ok = await checkAuth();
  await loadSites();
  if (ok) {
    load();
    // 订阅新运行启动事件：定时任务等后台触发的 scheduled 运行也能实时接入
    watchNewRuns();
  }
})();
