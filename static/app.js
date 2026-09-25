/* 批次放行复核台前端：纯原生 JS，无构建依赖。 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const stateLabel = {manufactured: "已生产", investigation: "调查中", awaiting_resample: "待再取样",
  conditional: "有条件放行", released: "已放行", rejected: "已拒绝"};
const verdictLabel = {blocked: ["🚫 卡住", "b-blocked"], conditional: ["⚠️ 可条件放行", "b-conditional"],
  ready: ["✅ 可放行", "b-ready"]};
const catLabel = {deviation: "偏差", test: "检验", rework: "返工", supplier: "供应商变更", stability: "稳定性"};

let desk = null;
const expanded = new Set();   // 展开的批次
const detailCache = new Map();

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}
function identity() {
  const [role, actor] = $("#actor").value.split(":");
  return {role, actor};
}
async function api(path, body) {
  const {actor, role} = identity();
  const res = await fetch(path, {
    method: "POST", headers: {"Content-Type": "application/json", "X-Actor": actor, "X-Role": role},
    body: JSON.stringify(body || {}),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `请求失败 ${res.status}`);
  return data;
}
function toast(msg, ok = true) {
  const div = document.createElement("div");
  div.className = ok ? "ok" : "err";
  div.textContent = msg;
  $("#toast").appendChild(div);
  setTimeout(() => div.remove(), 4200);
}

// ---------------- 复核台加载 ----------------
async function load() {
  const params = new URLSearchParams();
  const v = $("#tabs button.on").dataset.v;
  if (v) params.set("verdict", v);
  if ($("#f-status").value) params.set("status", $("#f-status").value);
  const p = $("#f-product").value.trim();
  if (p) params.set("product", p);
  desk = await (await fetch("/api/desk?" + params.toString())).json();
  detailCache.clear();
  renderStats();
  renderList();
}

function renderStats() {
  const s = desk.stats;
  $("#stats").innerHTML = [
    ["待办批次", s.total], ["卡住", s.blocked], ["可条件放行", s.conditional], ["可放行", s.ready],
    ["复核已失效", s.review_stale], ["已终态", s.terminal],
  ].map(([k, n]) => `<div class="stat"><b>${n}</b>${k}</div>`).join("");
}

function renderList() {
  const list = $("#list");
  if (!desk.cards.length) { list.innerHTML = '<div class="empty">没有符合筛选条件的批次</div>'; return; }
  list.innerHTML = "";
  for (const card of desk.cards) list.appendChild(renderCard(card));
}

function renderCard(card) {
  const b = card.batch, ev = card.evaluation;
  const [vText, vCls] = verdictLabel[ev.verdict];
  const el = document.createElement("div");
  el.className = `card s-v-${ev.verdict}` + (expanded.has(b.id) ? " open" : "");
  const hard = ev.counts.hard_blocking, soft = ev.counts.blocking - hard;
  el.innerHTML = `
    <div class="card-head">
      <span class="chev">▶</span>
      <span class="no">${esc(b.batch_no)}</span>
      <span class="meta">${esc(b.product)} · ${esc(b.factory_code || ("工厂#" + b.factory_id))} ·
        生产日期 ${esc(b.mfg_date)} · 效期 ${esc(b.expiry_date)} · 资料版本 <code class="rev">v${b.revision}</code>
        ${hard ? `· <b style="color:var(--red)">${hard} 项硬缺项</b>` : ""}
        ${soft ? `· <b style="color:var(--amber)">${soft} 项凭例外挂起</b>` : ""}</span>
      <span class="badge ${vCls}">${vText}</span>
      <span class="badge b-state">${stateLabel[b.state] || b.state}</span>
    </div>
    <div class="card-body"></div>`;
  el.querySelector(".card-head").onclick = () => toggleCard(b.id, el);
  if (expanded.has(b.id)) fillBody(el.querySelector(".card-body"), card);
  return el;
}

async function toggleCard(bid, el) {
  const open = el.classList.toggle("open");
  if (open) {
    expanded.add(bid);
    const card = desk.cards.find(c => c.batch.id === bid);
    await fillBody(el.querySelector(".card-body"), card);
  } else expanded.delete(bid);
}

async function getDetail(bid) {
  if (!detailCache.has(bid)) detailCache.set(bid, await (await fetch(`/api/batches/${bid}`)).json());
  return detailCache.get(bid);
}

// ---------------- 卡片展开内容 ----------------
async function fillBody(root, card) {
  const b = card.batch;
  const d = await getDetail(b.id);
  root.innerHTML = "";
  root.appendChild(renderReviewStrip(card));
  root.appendChild(renderMissing(card, d));

  const grid = document.createElement("div");
  grid.className = "grid";
  grid.appendChild(section("最新检验（每项目只看最新轮次）", testsTable(card.latest_tests)));
  grid.appendChild(section("最新稳定性（每条件只看最新时间点）", stabilityTable(card.latest_stability)));
  grid.appendChild(section("偏差记录", deviationsTable(d.deviations)));
  grid.appendChild(section("返工 / 供应商变更", reworkTable(d.rework) + supplierTable(d.supplier_changes)));
  root.appendChild(grid);
  root.appendChild(renderActions(card));
}

function section(title, html) {
  const div = document.createElement("div");
  div.innerHTML = `<h4 class="sec">${esc(title)}</h4>${html}`;
  return div;
}

function passCell(ok) {
  return `<td class="${ok ? "pass" : "fail"}">${ok ? "合格" : "不合格"}</td>`;
}
function testsTable(rows) {
  if (!rows.length) return '<p class="hint">无检验记录</p>';
  return `<table><tr><th>项目</th><th>轮次</th><th>结果</th><th>限度</th><th>判定</th></tr>` +
    rows.map(t => `<tr><td>${esc(t.test_type)}</td><td>第 ${t.round} 轮</td>
      <td class="${t.passed ? "" : "fail"}">${t.result}</td><td>[${t.spec_min}, ${t.spec_max}]</td>${passCell(t.passed)}</tr>`).join("") + "</table>";
}
function stabilityTable(rows) {
  if (!rows.length) return '<p class="hint">暂无稳定性数据（提示项，不硬挡）</p>';
  return `<table><tr><th>条件</th><th>时间点</th><th>结果</th><th>限度</th><th>判定</th></tr>` +
    rows.map(s => `<tr><td>${esc(s.condition)}</td><td>${esc(s.timepoint)}</td>
      <td class="${s.passed ? "" : "fail"}">${s.result}</td><td>≤ ${s.spec_limit}</td>${passCell(s.passed)}</tr>`).join("") + "</table>";
}
function deviationsTable(rows) {
  if (!rows.length) return '<p class="hint">无偏差</p>';
  return `<table><tr><th>#</th><th>等级</th><th>标题</th><th>状态/例外</th></tr>` +
    rows.map(x => `<tr><td>${x.id}</td>
      <td>${x.severity === "critical" ? '<b class="pill-fail">关键</b>' : "一般"}</td>
      <td>${esc(x.title)}${x.corrective_action ? `<br><small class="hint">纠正：${esc(x.corrective_action)}</small>` : ""}</td>
      <td>${x.status === "closed" ? "已关闭" : "未关闭"}${x.exception_until ? `<br><small class="hint">例外至 ${esc(x.exception_until)}<br>${esc(x.exception_reason || "")}</small>` : ""}</td></tr>`).join("") + "</table>";
}
function reworkTable(rows) {
  if (!rows.length) return "";
  return rows.map(r => `<p class="hint">返工 #${r.id}：${esc(r.description)} — ${r.status === "completed" ? "✅ 已完成" : "⏳ 已安排未完成"}</p>`).join("");
}
function supplierTable(rows) {
  if (!rows.length) return '<p class="hint">无供应商变更</p>';
  return `<table><tr><th>#</th><th>供应商/变更</th><th>影响处置</th></tr>` +
    rows.map(s => `<tr><td>${s.id}</td><td>${esc(s.supplier)}（${esc(s.change_type)}）<br><small class="hint">${esc(s.description)}</small></td>
      <td>${s.disposition === "assessed" ? `✅ 已评估<br><small class="hint">${esc(s.impact_assessment || "")}</small>` : '<b class="pill-fail">未处置</b>'}</td></tr>`).join("") + "</table>";
}

// ---------------- 缺项清单 ----------------
function renderMissing(card, detail) {
  const wrap = document.createElement("div");
  const items = card.missing;
  if (!items.length) {
    wrap.innerHTML = '<h4 class="sec">缺项 / 待办</h4><p style="color:var(--green);margin:4px 0 10px">🎉 无缺项：未关闭偏差、检验、返工、供应商变更与稳定性全部满足</p>';
    return wrap;
  }
  wrap.innerHTML = `<h4 class="sec">缺项 / 待办（${items.length}）</h4>`;
  const ul = document.createElement("ul");
  ul.className = "missing";
  for (const it of items) ul.appendChild(missingLi(it, card, detail));
  wrap.appendChild(ul);
  return wrap;
}

function missingLi(it, card, detail) {
  const icon = it.warning ? "🔔" : (it.critical ? "⛔" : {deviation: "📌", test: "🧪", rework: "🔧", supplier: "🚚", stability: "📈"}[it.category] || "•");
  const li = document.createElement("li");
  li.className = `cat-${it.category}` + (it.warning ? " warning" : "");
  const txt = document.createElement("div");
  txt.className = "txt";
  txt.innerHTML = `<b>${esc(it.title)}</b> <small>[${catLabel[it.category]}] ${esc(it.detail)}</small>`;
  li.innerHTML = `<span class="ic">${icon}</span>`;
  li.appendChild(txt);
  const acts = document.createElement("span");
  acts.className = "acts";

  const b = card.batch, d = detail;
  if (it.category === "deviation") {
    const dev = d.deviations.find(x => "deviation:" + x.id === it.key);
    if (!it.close_ready) {
      const note = document.createElement("div");
      note.className = "close-note";
      note.textContent = "关闭前置未满足：" + it.close_reasons.join("；");
      txt.appendChild(note);
    }
    if (dev && dev.severity === "minor") {
      acts.appendChild(btn("批准例外", "mini", () => openForm("exception", {batch: b, dev})));
    }
    acts.appendChild(btn(it.close_ready ? "关闭偏差" : "关闭（前置未满足）", "mini",
      () => { if (it.close_ready) openForm("close", {batch: b, dev}); }, !it.close_ready));
  } else if (it.category === "test") {
    const name = it.key.startsWith("test:") ? it.key.slice(5) : "";
    const pre = card.latest_tests.find(t => t.test_type === name);
    acts.appendChild(btn("登记合格复测", "mini", () => openForm("test", {batch: b, pre})));
  } else if (it.category === "rework") {
    const rw = d.rework.find(x => "rework:" + x.id === it.key);
    acts.appendChild(btn("完成返工", "mini", () => openForm("complete_rework", {batch: b, rw})));
  } else if (it.category === "supplier") {
    const sc = d.supplier_changes.find(x => "supplier:" + x.id === it.key);
    acts.appendChild(btn("影响处置", "mini", () => openForm("dispose", {batch: b, sc})));
  } else if (it.category === "stability" && it.blocking) {
    acts.appendChild(btn("补录稳定性", "mini", () => openForm("stability", {batch: b})));
  } else if (it.key === "tests:none") {
    acts.appendChild(btn("登记检验", "mini", () => openForm("test", {batch: b})));
  } else if (it.warning) {
    acts.appendChild(btn("补录稳定性", "mini", () => openForm("stability", {batch: b})));
  }
  li.appendChild(acts);
  return li;
}

function btn(text, cls, onclick, disabled = false) {
  const b = document.createElement("button");
  b.textContent = text;
  if (cls) b.className = cls;
  b.disabled = disabled;
  b.onclick = e => { e.stopPropagation(); onclick(); };
  return b;
}

// ---------------- 复核条与历史 ----------------
function renderReviewStrip(card) {
  const b = card.batch, ar = card.active_review, lr = card.last_review;
  const strip = document.createElement("div");
  const hist = document.createElement("div");
  hist.className = "hist";
  hist.style.margin = "0 0 10px";
  if (["released", "rejected"].includes(b.state)) {
    strip.className = "review-strip none";
    strip.innerHTML = `<span>🔒</span><span class="grow">批次已终态（${stateLabel[b.state]}），资料锁定；放行所依据的复核与决定见历史</span>`;
  } else if (ar && ar.revision === b.revision) {
    strip.className = "review-strip ok";
    strip.innerHTML = `<span>✅</span><span class="grow">当前版本 <code class="rev">v${ar.revision}</code> 复核有效：
      <b>${verdictLabel[ar.verdict][0]}</b> — ${esc(ar.conclusion)} <small class="hint">（${esc(ar.reviewed_by)} ${esc(ar.created_at)}）</small></span>`;
  } else if (lr) {
    strip.className = "review-strip stale";
    strip.innerHTML = `<span>♻️</span><span class="grow">资料已修改：原 v${lr.revision} 复核结论（${esc((lr.conclusion || "").slice(0, 40))}）已失效，
      需按当前版本 <code class="rev">v${b.revision}</code> 重新复核；旧结论仍可在下方历史查阅</span>`;
  } else {
    strip.className = "review-strip none";
    strip.innerHTML = `<span>📝</span><span class="grow">当前版本 <code class="rev">v${b.revision}</code> 尚无复核结论</span>`;
  }
  const histBtn = document.createElement("button");
  histBtn.className = "mini"; histBtn.textContent = "历史复核结论";
  histBtn.onclick = async () => {
    const r = await (await fetch(`/api/batches/${b.id}/reviews`)).json();
    hist.innerHTML = r.reviews.length ? r.reviews.map(x => `
      <div class="old"><b>${x.status === "active" ? "有效" : "已失效"}</b> · v${x.revision} ·
        ${verdictLabel[x.verdict][0]} · ${esc(x.reviewed_by)} · ${esc(x.created_at)}
        ${x.superseded_at ? `<br><small>失效于 ${esc(x.superseded_at)}</small>` : ""}<br>${esc(x.conclusion)}</div>`).join("")
      : '<p class="hint">暂无历史复核</p>';
  };
  strip.appendChild(histBtn);
  const wrap = document.createElement("div");
  wrap.appendChild(strip); wrap.appendChild(hist);
  return wrap;
}

// ---------------- 批次级操作 ----------------
function renderActions(card) {
  const b = card.batch, ev = card.evaluation, terminal = ["released", "rejected"].includes(b.state);
  const row = document.createElement("div");
  row.className = "actions-row";
  const add = (text, key, primary = false) => {
    const x = btn(text, primary ? "primary" : "", () => openForm(key, {batch: b}));
    if (terminal) x.disabled = true;
    row.appendChild(x);
  };
  add("登记偏差", "deviation");
  add("登记检验/复测", "test");
  add("补录稳定性", "stability");
  add("安排返工", "rework");
  add("供应商变更", "supplier");
  add("♻️ 登记复核结论", "review", true);

  if (!terminal) {
    const release = btn("✅ 正式放行", "primary", () => openForm("decide", {batch: b, decision: "release"}));
    release.disabled = ev.verdict !== "ready";
    const cond = btn("⚠️ 有条件放行", "", () => openForm("decide", {batch: b, decision: "conditional"}));
    cond.disabled = ev.verdict !== "conditional";
    const resample = btn("再取样", "", () => openForm("decide", {batch: b, decision: "resample"}));
    const reject = btn("拒绝批次", "danger", () => openForm("decide", {batch: b, decision: "reject"}));
    row.append(release, cond, resample, reject);
  } else {
    const done = document.createElement("span");
    done.className = "hint";
    done.textContent = "批次已终态，资料锁定。";
    row.appendChild(done);
  }
  return row;
}

// ---------------- 通用弹窗表单 ----------------
const dlg = $("#dlg");
let pendingSubmit = null;

const F = {
  text: (name, label, def = "", required = true) => ({name, label, type: "text", def, required}),
  textarea: (name, label, def = "", required = true) => ({name, label, type: "textarea", def, required}),
  number: (name, label, def = "", required = true) => ({name, label, type: "number", def, required}),
  date: (name, label, def = "", required = true) => ({name, label, type: "date", def, required}),
  select: (name, label, options, def) => ({name, label, type: "select", options, def, required: true}),
};

function futureDate() {
  return new Date(Date.now() + 30 * 864e5).toISOString().slice(0, 10);
}

function formDef(key, ctx) {
  const bid = ctx.batch ? ctx.batch.id : null;
  // 提交瞬间实时取修订号，避免展开卡片后资料被他人修改导致 409
  const fresh = async () => {
    const d = await (await fetch(`/api/batches/${bid}`)).json();
    return {revision: d.batch.revision, factory_id: d.batch.factory_id};
  };
  const wrap = (url, fields, title) => ({
    title, fields, submit: async v => {
      const f = await fresh();
      return api(url, {expected_revision: f.revision, factory_id: f.factory_id, ...v});
    },
  });
  switch (key) {
    case "create":
      return {title: "登记批次", fields: [
        F.number("factory_id", "工厂 ID（演示工厂为 1）", 1),
        F.text("batch_no", "批号"), F.text("product", "产品名称"),
        F.date("mfg_date", "生产日期", "2026-07-01"), F.date("expiry_date", "有效期至", "2028-07-01"),
      ], submit: v => api("/api/batches", {
        ...v, factory_id: Number(v.factory_id),
        mfg_date: v.mfg_date, expiry_date: v.expiry_date})};
    case "deviation":
      return wrap(`/api/batches/${bid}/deviations`, [
        F.select("severity", "偏差等级", [["minor", "一般"], ["critical", "关键"]], "minor"),
        F.text("title", "偏差标题"), F.date("due_at", "整改期限", futureDate(), false),
      ], "登记偏差");
    case "close":
      return {title: `关闭偏差 #${ctx.dev.id}`, fields: [F.textarea("corrective_action", "纠正措施 / 调查结论")],
        submit: async v => {
          const f = await fresh();
          return api(`/api/deviations/${ctx.dev.id}/close`, {expected_revision: f.revision, ...v});
        }};
    case "exception":
      return {title: `为一般偏差 #${ctx.dev.id} 批准例外`, fields: [
        F.textarea("exception_reason", "例外理由（关键偏差不允许）"), F.date("until", "例外有效期至", futureDate()),
      ], submit: async v => {
        const f = await fresh();
        return api(`/api/deviations/${ctx.dev.id}/exception`, {
          expected_revision: f.revision, reason: v.exception_reason, until: v.until});
      }};
    case "test":
      return wrap(`/api/batches/${bid}/tests`, [
        F.text("test_type", "检验项目", ctx.pre?.test_type || "含量"),
        F.number("result", "检验结果", ctx.pre?.result ?? ""),
        F.number("spec_min", "下限", ctx.pre?.spec_min ?? 95),
        F.number("spec_max", "上限", ctx.pre?.spec_max ?? 105),
      ], "登记检验 / 复测（轮次自动递增）");
    case "stability":
      return wrap(`/api/batches/${bid}/stability`, [
        F.text("condition", "考察条件", "25C/60RH"), F.text("timepoint", "时间点", "6M"),
        F.number("result", "结果", 100), F.number("spec_limit", "限度（≤）", 105),
      ], "补录稳定性数据");
    case "rework":
      return wrap(`/api/batches/${bid}/rework`, [F.textarea("description", "返工内容")], "安排返工");
    case "complete_rework":
      return {title: `完成返工 #${ctx.rw.id}`, fields: [
        {name: "ok", label: `返工内容：${ctx.rw.description}`, type: "text", def: "确认返工完成并记录", required: false},
      ], submit: async () => {
        const f = await fresh();
        return api(`/api/rework/${ctx.rw.id}/complete`, {expected_revision: f.revision, factory_id: f.factory_id});
      }};
    case "supplier":
      return wrap(`/api/batches/${bid}/supplier-changes`, [
        F.text("supplier", "供应商"),
        F.select("change_type", "变更类型", [["原料药供应商变更", "原料药供应商"], ["内包材变更", "内包材"], ["辅料供应商变更", "辅料"], ["场地变更", "场地"]], "原料药供应商变更"),
        F.textarea("description", "变更说明"),
      ], "登记供应商变更（未做影响处置将留在待办）");
    case "dispose":
      return {title: `供应商变更 #${ctx.sc.id} 影响处置`, fields: [
        F.textarea("impact_assessment", "影响评估 / 处置结论（桥接、验证、质量协议等）"),
      ], submit: async v => {
        const f = await fresh();
        return api(`/api/supplier-changes/${ctx.sc.id}/dispose`, {expected_revision: f.revision, factory_id: f.factory_id, ...v});
      }};
    case "review":
      return {title: `登记复核结论（批次 ${ctx.batch.batch_no} · v${ctx.batch.revision}）`, fields: [
        F.textarea("conclusion", "复核意见（缺项将由系统按当前版本重算）"),
      ], submit: v => api("/api/reviews", {batch_id: bid, ...v})};  // 复核本身不推进修订号
    case "decide": {
      const map = {release: "正式放行", conditional: "有条件放行", resample: "再取样", reject: "拒绝批次"};
      const fields = [F.textarea("rationale", "决定依据")];
      if (ctx.decision === "conditional") fields.push(F.text("exception_code", "例外编号", "EX-"));
      return {title: `${map[ctx.decision]}：${ctx.batch.batch_no}`, fields,
        submit: async v => {
          const f = await fresh();
          return api(`/api/batches/${bid}/decide`, {expected_revision: f.revision, decision: ctx.decision, ...v});
        }};
    }
  }
}

function openForm(key, ctx = {}) {
  const def = formDef(key, ctx);
  $("#dlg-title").textContent = def.title;
  const body = $("#dlg-body");
  body.innerHTML = "";
  for (const f of def.fields) {
    const label = document.createElement("label");
    label.innerHTML = esc(f.label);
    let input;
    if (f.type === "textarea") {
      input = document.createElement("textarea"); input.rows = 3;
    } else if (f.type === "select") {
      input = document.createElement("select");
      for (const [val, t] of f.options) input.add(new Option(t, val));
    } else {
      input = document.createElement("input");
      input.type = f.type;
    }
    input.name = f.name;
    if (f.def !== undefined && f.def !== null) input.value = f.def;
    if (f.required) input.required = true;
    label.appendChild(input);
    body.appendChild(label);
  }
  pendingSubmit = def.submit;
  dlg.showModal();
  setTimeout(() => body.querySelector("input,textarea,select")?.focus(), 0);
}

$("#dlg-ok").onclick = async () => {
  const values = {};
  let bad = false;
  for (const f of $("#dlg-body").querySelectorAll("input,textarea,select")) {
    values[f.name] = f.type === "number" ? (f.value === "" ? "" : Number(f.value)) : f.value;
    if (f.required && !String(values[f.name]).trim()) { bad = true; f.style.borderColor = "var(--red)"; }
  }
  if (bad) { toast("请填写必填项", false); return; }
  try {
    await pendingSubmit(values);
    toast("操作成功");
    dlg.close();
    await load();
  } catch (e) { toast(e.message, false); }
};

// ---------------- 筛选事件 ----------------
$("#tabs").addEventListener("click", e => {
  if (e.target.tagName !== "BUTTON") return;
  $("#tabs button").forEach(x => x.classList.remove("on"));
  e.target.classList.add("on");
  load();
});
$("#f-status").onchange = load;
let timer;
$("#f-product").oninput = () => { clearTimeout(timer); timer = setTimeout(load, 300); };

load().catch(e => toast(e.message, false));
