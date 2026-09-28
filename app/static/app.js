/* 签到控制中心前端：vanilla JS + fetch */
const $ = (s) => document.querySelector(s);
let pollTimer = null;
let editingId = null;

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.style.display = "block";
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.style.display = "none"), 2600);
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || data.error || `请求失败 (${r.status})`);
  return data;
}

function fmtNext(iso) {
  if (!iso) return "自动签到未开启";
  const d = new Date(iso);
  const p = (n) => String(n).padStart(2, "0");
  return `下次运行：${d.getMonth() + 1}月${d.getDate()}日 ${p(d.getHours())}:${p(d.getMinutes())}（北京时间）`;
}

function fmtTs(ts) {
  if (!ts) return "";
  // "2026-09-29T06:07:04+08:00" -> "06:07:04"
  const m = ts.match(/T(\d{2}:\d{2}:\d{2})/);
  if (m) {
    const d = ts.slice(0, 10);
    return `${d.slice(5)} ${m[1]}`;
  }
  return ts;
}

function todayBadge(t) {
  if (!t) return `<span class="badge wait">今日未签到</span>`;
  if (t.status === "success") return `<span class="badge ok">今日已签到</span>`;
  return `<span class="badge err">今日失败</span>`;
}

function renderAccounts(accounts) {
  const box = $("#accounts");
  if (!accounts.length) {
    box.innerHTML = `<div class="muted">还没有账号，点击「添加账号」录入第一个吧。凭据只保存在本机加密数据库中。</div>`;
    return;
  }
  box.innerHTML = accounts
    .map((a) => {
      const t = a.today;
      const metric = t && t.metric_label
        ? `<span>${esc(t.metric_label)} <b>${esc(t.metric_value ?? "—")}</b></span>` : "";
      const streak = t && t.streak_days != null
        ? `<span>连签 <b>${t.streak_days}</b> 天</span>` : "";
      return `
      <div class="acct ${a.enabled ? "" : "disabled"}" data-id="${a.id}">
        <div class="row" style="justify-content:space-between">
          <span class="site">${esc(a.siteName)}</span>
          <input type="checkbox" class="pick" data-id="${a.id}" ${a.enabled ? "" : "disabled"} title="勾选后手动签到">
        </div>
        <div class="label">${esc(a.nickname || a.accountLabel)}</div>
        ${a.nickname ? `<div class="nick">${esc(a.accountLabel)}</div>` : ""}
        <div class="today">${todayBadge(t)}${t && t.conclusion ? ` <span class="muted">${esc(t.conclusion)}</span>` : ""}</div>
        <div class="metrics">${metric}${streak}
          ${a.credentialConfigured ? "" : `<span class="badge err">凭据未配置</span>`}
          ${a.enabled ? "" : `<span class="badge off">已停用</span>`}
        </div>
        <div class="actions">
          <button data-act="edit">编辑</button>
          <button data-act="toggle">${a.enabled ? "停用" : "启用"}</button>
          <button data-act="del" class="danger">删除</button>
        </div>
      </div>`;
    })
    .join("");
  box.querySelectorAll(".pick").forEach((c) =>
    c.addEventListener("change", updateSelCount)
  );
  box.querySelectorAll(".acct button").forEach((b) =>
    b.addEventListener("click", onAcctAction)
  );
  updateSelCount();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function updateSelCount() {
  const n = document.querySelectorAll(".pick:checked").length;
  $("#sel-count").textContent = n;
  $("#btn-manual").disabled = n === 0;
}

function renderRuns(runs) {
  const box = $("#runs");
  if (!runs.length) {
    box.innerHTML = `<div class="muted">暂无运行记录。</div>`;
    return;
  }
  box.innerHTML = runs
    .map((r) => {
      const mode = r.mode === "scheduled" ? "自动签到" : "手动签到";
      const st = r.status === "running"
        ? `<span class="badge wait">进行中</span>`
        : r.status === "failed"
        ? `<span class="badge err">失败</span>`
        : `<span class="badge ok">已完成</span>`;
      const sum = r.total
        ? `<span class="muted small">${r.succeeded} 成功 / ${r.failed} 失败</span>` : "";
      return `
      <div class="run" data-id="${r.id}">
        <div class="head">
          <b>${mode}</b>${st}${sum}
          <span class="muted small">${fmtTs(r.created_at)}</span>
          <span class="muted small">▾ 展开</span>
        </div>
        <div class="detail"><div class="spin"></div> 加载中…</div>
      </div>`;
    })
    .join("");
  box.querySelectorAll(".run .head").forEach((h) =>
    h.addEventListener("click", () => toggleRun(h.parentElement))
  );
}

async function toggleRun(el) {
  el.classList.toggle("open");
  if (!el.classList.contains("open")) return;
  const id = el.dataset.id;
  const detail = el.querySelector(".detail");
  try {
    const run = await api(`/api/runs/${id}`);
    detail.innerHTML = renderRunDetail(run);
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
        <td>${r.status === "success" ? '<span class="badge ok">成功</span>' : '<span class="badge err">失败</span>'}</td>
        <td>${esc(r.conclusion || "")}</td>
        <td>${esc(r.metric_label || "")} ${esc(r.metric_value ?? "")}</td>
        <td>${r.streak_days ?? "—"}</td>
      </tr>`)
    .join("");
  return `
    ${run.error ? `<div class="badge err">${esc(run.error)}</div>` : ""}
    <div class="timeline">${steps || '<div class="muted small">暂无步骤</div>'}</div>
    ${rows ? `<table><tr><th>站点</th><th>账号</th><th>状态</th><th>结论</th><th>余额</th><th>连签</th></tr>${rows}</table>` : ""}`;
}

function siteName(k) {
  return { jiaobenwang: "脚本王", pikaqiu: "皮卡丘token商店", ebondai: "ebondai" }[k] || k;
}

async function load() {
  const d = await api("/api/dashboard");
  $("#set-enabled").checked = d.autoCheckin.enabled;
  $("#set-time").value = d.autoCheckin.time;
  $("#next-run").textContent = fmtNext(d.autoCheckin.nextRunAt);
  renderAccounts(d.accounts);
  renderRuns(d.runs);
  if (d.running) {
    // 刷新时如果有任务在跑，跟随最新的 run
    const latest = d.runs.find((r) => r.status === "running");
    if (latest) followRun(latest.id);
  }
}

/* ---------- 账号表单 ---------- */
function openModal(title, acct) {
  editingId = acct ? acct.id : null;
  $("#modal-title").textContent = title;
  $("#f-site").value = acct ? acct.siteKey : "jiaobenwang";
  $("#f-site").disabled = !!acct;
  $("#f-nick").value = acct ? acct.nickname || "" : "";
  $("#f-login").value = "";
  $("#f-password").value = "";
  $("#f-apikey").value = "";
  $("#f-login").placeholder = acct ? "留空表示不更换" : "user@example.com";
  $("#f-enabled").checked = acct ? acct.enabled : true;
  syncKeyField();
  $("#modal").classList.add("show");
}
function closeModal() { $("#modal").classList.remove("show"); }
function syncKeyField() {
  $("#key-field").style.display = $("#f-site").value === "ebondai" ? "" : "none";
}

async function onAcctAction(e) {
  const id = e.target.closest(".acct").dataset.id;
  const act = e.target.dataset.act;
  if (act === "edit") {
    const d = await api("/api/dashboard");
    const acct = d.accounts.find((a) => a.id === id);
    openModal("编辑账号", acct);
  } else if (act === "toggle") {
    const card = e.target.closest(".acct");
    const enabling = card.classList.contains("disabled");
    if (!enabling && !confirm("停用后该账号不再参与自动签到，手动签到时也会置灰。确定停用？")) return;
    await api(`/api/accounts/${id}/toggle`, {
      method: "POST", body: JSON.stringify({ enabled: enabling }),
    });
    toast(enabling ? "账号已启用" : "账号已停用");
    load();
  } else if (act === "del") {
    // 二次确认：删除凭据但保留历史记录中的账号标签
    if (!confirm("删除后该账号的凭据会被彻底删除（历史记录中的账号标签会保留）。确定删除吗？")) return;
    if (!confirm("再次确认：真的要删除这个账号吗？此操作不可撤销。")) return;
    await api(`/api/accounts/${id}`, { method: "DELETE" });
    toast("账号已删除");
    load();
  }
}

/* ---------- 运行跟随 ---------- */
function followRun(runId) {
  $("#live-card").style.display = "";
  $("#live-timeline").innerHTML = "";
  $("#live-results").innerHTML = "";
  clearInterval(pollTimer);
  const seen = new Set();
  pollTimer = setInterval(async () => {
    try {
      const run = await api(`/api/runs/${runId}`);
      const tl = $("#live-timeline");
      (run.steps || []).forEach((s, i) => {
        const key = i + "|" + s.ts + "|" + s.message;
        if (seen.has(key)) return;
        seen.add(key);
        const div = document.createElement("div");
        div.className = "step";
        div.innerHTML = `<span class="ts">${fmtTs(s.ts)}</span>${esc(s.message)}`;
        tl.appendChild(div);
      });
      if (run.status !== "running") {
        clearInterval(pollTimer);
        $("#live-results").innerHTML = renderRunDetail(run);
        toast(`签到完成：${run.succeeded} 成功 / ${run.failed} 失败`);
        setTimeout(load, 800);
      }
    } catch (e) {
      clearInterval(pollTimer);
      toast(e.message);
    }
  }, 2000);
}

/* ---------- 事件绑定 ---------- */
$("#btn-refresh").addEventListener("click", load);
$("#btn-save-settings").addEventListener("click", async () => {
  try {
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        enabled: $("#set-enabled").checked,
        time: $("#set-time").value,
      }),
    });
    toast("自动签到设置已保存，1 分钟内生效");
    load();
  } catch (e) { toast(e.message); }
});
$("#btn-run-now").addEventListener("click", async () => {
  if (!confirm("立即对全部已启用账号执行一次自动签到？")) return;
  try {
    const r = await api("/api/runs/now", { method: "POST" });
    toast(r.message);
    followRun(r.runId);
  } catch (e) { toast(e.message); }
});
$("#btn-manual").addEventListener("click", async () => {
  const ids = [...document.querySelectorAll(".pick:checked")].map((c) => c.dataset.id);
  try {
    const r = await api("/api/runs/manual", {
      method: "POST", body: JSON.stringify({ accountIds: ids }),
    });
    toast(r.message);
    followRun(r.runId);
  } catch (e) { toast(e.message); }
});
$("#btn-add").addEventListener("click", () => openModal("添加账号", null));
$("#btn-cancel").addEventListener("click", closeModal);
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
$("#f-site").addEventListener("change", syncKeyField);
$("#btn-save-acct").addEventListener("click", async () => {
  const login = $("#f-login").value.trim();
  const password = $("#f-password").value;
  const apiKey = $("#f-apikey").value.trim();
  try {
    if (editingId) {
      await api(`/api/accounts/${editingId}`, {
        method: "PUT",
        body: JSON.stringify({
          nickname: $("#f-nick").value.trim() || null,
          enabled: $("#f-enabled").checked,
          replaceCredentials: !!(login || password || apiKey),
          login: login || null, password: password || null,
          apiKey: apiKey || null,
        }),
      });
      toast("账号已更新");
    } else {
      await api("/api/accounts", {
        method: "POST",
        body: JSON.stringify({
          siteKey: $("#f-site").value,
          nickname: $("#f-nick").value.trim() || null,
          login: login || null, password: password || null,
          apiKey: apiKey || null,
          enabled: $("#f-enabled").checked,
        }),
      });
      toast("账号已添加");
    }
    closeModal();
    load();
  } catch (e) { toast(e.message); }
});

load().catch((e) => toast("加载失败：" + e.message));
