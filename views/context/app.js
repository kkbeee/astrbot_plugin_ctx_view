const bridge = window.AstrBotPluginView;
const el = (id) => document.getElementById(id);

const state = {
  tab: "sessions",
  data: null,
  stats: null,
  cid: null,
  query: "",
  days: 14,
  basis: "official",
  timer: null,
  loading: false,
  statsLoading: false,
};

/* ---------------------------------------------------------------- 格式化 */

const trimZero = (text) => text.replace(/\.?0+$/, "");

function formatTokens(value) {
  if (value == null) return "—";
  const n = Math.max(0, Math.round(value));
  if (n >= 1000000) return `${trimZero((n / 1000000).toFixed(2))}M`;
  if (n >= 10000) return `${trimZero((n / 1000).toFixed(1))}K`;
  return n.toLocaleString("zh-CN");
}

function formatMoney(value) {
  if (value == null) return "—";
  if (value <= 0) return "¥0";
  if (value < 0.01) return `¥${value.toFixed(4)}`;
  if (value < 1) return `¥${trimZero(value.toFixed(3))}`;
  return `¥${value.toFixed(2)}`;
}

function formatRate(value) {
  if (value == null || Number.isNaN(value)) return "—";
  const n = Math.round(value * 10) / 10;
  return Number.isInteger(n) ? `${n}%` : `${n.toFixed(1)}%`;
}

function formatPrice(value) {
  return `¥${Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 4 })}`;
}

function formatDuration(seconds) {
  if (!seconds) return "—";
  const n = Math.round(seconds);
  if (n < 60) return `${n}s`;
  return `${Math.floor(n / 60)}m${n % 60}s`;
}

/* ---------------------------------------------------------------- 概览 */

function renderSummary(summary) {
  if (!summary) return;
  const today = summary.today || {};
  const week = summary.week || {};
  const month = summary.month || {};
  const all = summary.all || {};

  el("ov-today").textContent = formatMoney(today.cost);
  el("ov-today-note").textContent = `${today.requests || 0} 次 · 命中 ${formatRate(today.rate)}`;
  el("ov-week").textContent = formatMoney(week.cost);
  el("ov-week-note").textContent = `${week.requests || 0} 次 · 命中 ${formatRate(week.rate)}`;
  el("ov-month").textContent = formatMoney(month.cost);
  el("ov-month-note").textContent = `${month.requests || 0} 次 · 命中 ${formatRate(month.rate)}`;
  el("ov-all").textContent = formatMoney(all.cost);
  el("ov-all-note").textContent = `${(all.requests || 0).toLocaleString("zh-CN")} 次请求`;
  el("ov-rate").textContent = formatRate(all.rate);
  el("ov-rate-note").textContent = `省下约 ${formatMoney(all.saved)}`;

  const budget = summary.budget || {};
  const has = Boolean(budget.daily || budget.monthly);
  el("budget-strip").hidden = !has;
  if (has) {
    setBudget("day", budget.daily_used, budget.daily);
    setBudget("month", budget.monthly_used, budget.monthly);
  }

  const peak = summary.peak || {};
  const badge = el("peak");
  if (peak.enabled) {
    badge.hidden = false;
    badge.textContent = `${peak.label} · ${peak.until} 后切换`;
    badge.title = `峰时 ${peak.schedule || peak.windows}，谷时 ${trimZero(String(peak.discount * 10))} 折`;
    badge.classList.toggle("active", Boolean(peak.active));
  } else {
    badge.hidden = true;
  }
}

function setBudget(key, used, limit) {
  const fill = el(`budget-${key}`);
  const text = el(`budget-${key}-text`);
  const amount = el(`budget-${key}-amount`);
  if (!limit) {
    fill.style.width = "0";
    text.textContent = "未设置";
    amount.textContent = "";
    fill.className = "budget-fill";
    return;
  }
  const rate = (used / limit) * 100;
  fill.style.width = `${Math.min(100, Math.max(0, rate))}%`;
  fill.className = "budget-fill" + (rate >= 100 ? " over" : rate >= 80 ? " warn" : "");
  text.textContent = `${Math.round(rate)}%`;
  amount.textContent = `${formatMoney(used)} / ${formatMoney(limit)}`;
}

/* ---------------------------------------------------------------- 会话 */

function matches(item, query) {
  if (!query) return true;
  return [item.label, item.title, item.umo, item.kind].some((value) =>
    String(value || "").toLowerCase().includes(query),
  );
}

function buildRow(item, activeCid) {
  const row = document.createElement("button");
  row.type = "button";
  row.className = "session" + (item.cid === activeCid ? " active" : "");
  row.setAttribute("role", "listitem");

  const head = document.createElement("span");
  head.className = "session-head";

  const name = document.createElement("span");
  name.className = "session-name";
  name.textContent = item.label;
  name.title = item.label;

  const num = document.createElement("span");
  num.className = "session-num";
  num.textContent = item.has_stats
    ? formatTokens(item.ctx)
    : `≈${formatTokens(item.estimated_ctx)}`;

  head.append(name, num);

  const sub = document.createElement("span");
  sub.className = "session-sub";
  const bits = [`${item.messages.toLocaleString("zh-CN")} 条消息`];
  if (item.kind) bits.push(item.kind);
  if (item.updated) bits.push(item.updated);
  sub.textContent = bits.join(" · ");

  const foot = document.createElement("span");
  foot.className = "session-foot";
  const rate = document.createElement("span");
  rate.className = "session-rate";
  rate.textContent = item.has_stats ? `命中 ${formatRate(item.rate)}` : "无请求记录";
  const cost = document.createElement("span");
  cost.className = "session-cost";
  cost.textContent = item.cost_total != null ? `累计 ${formatMoney(item.cost_total)}` : "";
  foot.append(rate, cost);

  row.append(head, sub, foot);
  row.addEventListener("click", () => select(item.cid));
  return row;
}

function renderList() {
  const data = state.data;
  if (!data) return;
  const query = state.query.trim().toLowerCase();
  const sessions = (data.sessions || []).filter((item) => matches(item, query));
  const activeCid = data.selected ? data.selected.cid : null;
  const list = el("list");
  list.replaceChildren(...sessions.map((item) => buildRow(item, activeCid)));
  if (!sessions.length) {
    const hint = document.createElement("p");
    hint.className = "empty";
    hint.style.padding = "28px 12px";
    hint.textContent = query ? "没有匹配的会话" : "还没有会话记录";
    list.append(hint);
  }
}

function renderDetail() {
  const data = state.data;
  const selected = data ? data.selected : null;
  el("empty").hidden = Boolean(selected);
  el("detail").hidden = !selected;
  if (!selected) {
    el("empty").textContent =
      data && (data.sessions || []).length ? "在左侧选一个会话" : "还没有任何会话记录";
    return;
  }

  const model = selected.model || "模型未记录";
  el("name").textContent = selected.label;
  el("name").title = selected.label;
  el("model").textContent = model;
  el("model").title = model;

  const meta = [`${selected.messages.toLocaleString("zh-CN")} 条消息`];
  if (selected.kind) meta.push(selected.kind);
  if (selected.updated) meta.push(`更新 ${selected.updated}`);
  el("meta").textContent = meta.join(" · ");

  const capacity = data.max_ctx ? `上限 ${formatTokens(data.max_ctx)}` : "";

  if (selected.has_stats) {
    const ctx = selected.ctx || 0;
    el("ctx").textContent = formatTokens(ctx);
    el("ctx-note").textContent = data.max_ctx
      ? `占上限 ${formatRate((ctx / data.max_ctx) * 100)}`
      : "";
    el("rate").textContent = formatRate(selected.rate);
    el("rate-note").textContent =
      `${formatTokens(selected.hit)} 命中 / ${formatTokens(selected.miss)} 未命中`;
    el("cost").textContent = formatMoney(selected.cost);
    el("cost-note").textContent = `全未命中约 ${formatMoney(selected.cost_miss)}`;
    el("bar-hit").style.width = ctx ? `${(selected.hit / ctx) * 100}%` : "0";
    el("bar-miss").style.width = ctx ? `${(selected.miss / ctx) * 100}%` : "0";
  } else {
    el("ctx").textContent = `≈${formatTokens(selected.estimated_ctx)}`;
    el("ctx-note").textContent = "按消息字数估算";
    el("rate").textContent = "—";
    el("rate-note").textContent = "还没有请求记录";
    el("cost").textContent = "—";
    el("cost-note").textContent = "";
    el("bar-hit").style.width = "0";
    el("bar-miss").style.width = "0";
  }

  if (selected.cost_total != null) {
    el("cost-total").textContent = formatMoney(selected.cost_total);
    el("cost-total-note").textContent =
      `${(selected.requests_total || 0).toLocaleString("zh-CN")} 次请求 · 命中 ${formatRate(selected.rate_total)}`;
  } else {
    el("cost-total").textContent = "—";
    el("cost-total-note").textContent = "还没有请求记录";
  }

  el("capacity").textContent = capacity;
  renderRecent(selected.recent || []);
}

function cell(text, className) {
  const td = document.createElement("td");
  if (className) td.className = className;
  td.textContent = text;
  return td;
}

function renderRecent(rows) {
  const body = el("recent");
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 6;
    td.className = "empty-cell";
    td.textContent = "这个会话还没有请求记录";
    tr.append(td);
    body.replaceChildren(tr);
    return;
  }
  body.replaceChildren(
    ...rows.map((row) => {
      const tr = document.createElement("tr");
      tr.append(
        cell(row.at || "—"),
        cell(formatTokens(row.ctx), "num"),
        cell(formatRate(row.rate), "num"),
        cell(formatTokens(row.out), "num"),
        cell(formatMoney(row.cost), "num"),
        cell(row.model || "—", "model"),
      );
      return tr;
    }),
  );
}

/* ---------------------------------------------------------------- 统计 */

function axisMoney(value) {
  if (value <= 0) return "¥0";
  if (value >= 1) return `¥${trimZero(value.toFixed(1))}`;
  return formatMoney(value);
}

function renderTrend(series) {
  const svg = el("trend");
  const NS = "http://www.w3.org/2000/svg";
  const W = 720;
  const H = 220;
  const left = 42;
  const right = 12;
  const top = 16;
  const bottom = 34;
  const plotW = W - left - right;
  const plotH = H - top - bottom;

  svg.replaceChildren();
  const items = series || [];
  const max = Math.max(0.01, ...items.map((item) => item.cost || 0));
  const step = items.length ? plotW / items.length : plotW;
  const barW = Math.max(3, Math.min(30, step * 0.62));

  for (let g = 0; g <= 2; g += 1) {
    const y = top + (plotH * g) / 2;
    const line = document.createElementNS(NS, "line");
    line.setAttribute("x1", left);
    line.setAttribute("x2", W - right);
    line.setAttribute("y1", y);
    line.setAttribute("y2", y);
    line.setAttribute("class", "grid");
    svg.append(line);
    const label = document.createElementNS(NS, "text");
    label.setAttribute("x", left - 6);
    label.setAttribute("y", y + 4);
    label.setAttribute("text-anchor", "end");
    label.setAttribute("class", "axis");
    label.textContent = axisMoney(max * (1 - g / 2));
    svg.append(label);
  }

  items.forEach((item, index) => {
    const cost = item.cost || 0;
    const h = Math.max(cost > 0 ? 2 : 0, (cost / max) * plotH);
    const x = left + step * index + (step - barW) / 2;
    const y = top + plotH - h;
    const bar = document.createElementNS(NS, "rect");
    bar.setAttribute("x", x);
    bar.setAttribute("y", y);
    bar.setAttribute("width", barW);
    bar.setAttribute("height", h);
    bar.setAttribute("rx", Math.min(3, barW / 2));
    bar.setAttribute("class", "trend-bar" + (index === items.length - 1 ? " today" : ""));
    const title = document.createElementNS(NS, "title");
    title.textContent = `${item.day} ${item.label}\n${formatMoney(cost)} · ${item.requests} 次 · 命中 ${formatRate(item.rate)}`;
    bar.append(title);
    svg.append(bar);

    const show = items.length <= 14 || index % Math.ceil(items.length / 10) === 0 || index === items.length - 1;
    if (show) {
      const label = document.createElementNS(NS, "text");
      label.setAttribute("x", x + barW / 2);
      label.setAttribute("y", H - 12);
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("class", "axis");
      label.textContent = item.label;
      svg.append(label);
    }
  });
}

function renderRow(cols, className) {
  const tr = document.createElement("tr");
  cols.forEach((value, index) => {
    const isNum = index > 0 && typeof value === "string" && /^[¥\d]/.test(value);
    tr.append(cell(value, isNum || (index > 0 && className === "num") ? "num" : ""));
  });
  return tr;
}

function renderStats() {
  const data = state.stats;
  if (!data) return;
  const window = data.window || {};
  const models = data.by_model || [];
  const kinds = data.by_kind || [];
  const hours = data.by_hour || [];
  const failures = data.failures || [];
  const latency = data.latency || {};

  const peakSaved = window.peak_saved || 0;
  const basisNote = data.basis === "list" ? "原价口径 · 不含峰谷折扣" : "";
  el("trend-note").textContent =
    `近 ${data.days} 天 ${formatMoney(window.cost)} · ${(window.requests || 0).toLocaleString("zh-CN")} 次请求`
    + ` · 命中 ${formatRate(window.rate)} · 缓存省下 ${formatMoney(window.saved)}`
    + (peakSaved > 0 ? ` · 谷时省 ${formatMoney(peakSaved)}（原价 ${formatMoney(window.gross)}）` : "")
    + (basisNote ? ` · ${basisNote}` : "");
  renderTrend(data.series || []);
  renderSummary({
    today: data.today,
    week: data.week,
    month: data.month,
    all: data.all,
    budget: data.budget,
    peak: data.peak,
  });

  el("model-note").textContent = `共 ${models.length} 个`;
  el("model-rows").replaceChildren(
    ...(models.length
      ? models.map((item) =>
          renderRow([
            item.model,
            (item.requests || 0).toLocaleString("zh-CN"),
            formatMoney(item.cost),
            `${item.share || 0}%`,
            formatRate(item.rate),
          ]),
        )
      : [emptyRow(5, "这段时间没有请求")]),
  );

  el("kind-rows").replaceChildren(
    ...(kinds.length
      ? kinds.map((item) =>
          renderRow([
            item.kind,
            (item.requests || 0).toLocaleString("zh-CN"),
            formatMoney(item.cost),
            `${item.share || 0}%`,
          ]),
        )
      : [emptyRow(4, "这段时间没有请求")]),
  );

  renderHours(hours);

  el("latency-note").textContent = latency.n
    ? `平均 ${formatDuration(latency.avg)} · P50 ${formatDuration(latency.p50)} · P95 ${formatDuration(latency.p95)}`
    : "";
  el("fail-rows").replaceChildren(
    ...(failures.length
      ? failures.map((item) =>
          renderRow([item.at || "—", item.model || "—", item.kind || "—", item.status || "?"]),
        )
      : [emptyRow(4, "这段时间没有失败请求")]),
  );
}

function emptyRow(cols, text) {
  const tr = document.createElement("tr");
  const td = document.createElement("td");
  td.colSpan = cols;
  td.className = "empty-cell";
  td.textContent = text;
  tr.append(td);
  return tr;
}

function renderHours(hours) {
  const box = el("hour-chart");
  const max = Math.max(0.001, ...hours.map((item) => item.cost || 0));
  box.replaceChildren(
    ...hours.map((item) => {
      const col = document.createElement("div");
      col.className = "hour-col";
      col.title = `${String(item.hour).padStart(2, "0")}:00 · ${formatMoney(item.cost)} · ${item.requests} 次`;

      const bar = document.createElement("span");
      bar.className = "hour-bar";
      const ratio = (item.cost || 0) / max;
      bar.style.height = `${Math.max(item.cost > 0 ? 4 : 0, ratio * 100)}%`;

      const label = document.createElement("span");
      label.className = "hour-label";
      label.textContent = item.hour % 6 === 0 ? String(item.hour) : "";

      col.append(bar, label);
      return col;
    }),
  );
}

function exportCsv() {
  const data = state.stats;
  if (!data) return;
  const rows = [["日期", "请求", "上下文token", "命中token", "输出token", "命中率%", "花费元", "省下元"]];
  (data.series || []).forEach((item) => {
    rows.push([
      item.day,
      item.requests,
      item.ctx,
      item.hit,
      item.out,
      item.rate,
      item.cost,
      item.saved,
    ]);
  });
  rows.push([]);
  rows.push(["模型", "请求", "花费元", "占比%", "命中率%"]);
  (data.by_model || []).forEach((item) => {
    rows.push([item.model, item.requests, item.cost, item.share, item.rate]);
  });
  const text = rows
    .map((row) => row.map((value) => `"${String(value ?? "").replace(/"/g, '""')}"`).join(","))
    .join("\r\n");
  const blob = new Blob(["\ufeff" + text], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `ctx-cost-${data.series[data.series.length - 1]?.day || "export"}.csv`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ---------------------------------------------------------------- 取数 */

async function load(cid = state.cid) {
  if (!bridge) return;
  if (state.loading) return;
  state.loading = true;
  if (!state.data) el("empty").textContent = "正在读取…";
  try {
    const raw = await bridge.apiGet("overview", { cid: cid || "", recent: 10, basis: state.basis });
    const data = raw && typeof raw === "object" && "sessions" in raw ? raw : raw && raw.data;
    if (!data || !Array.isArray(data.sessions)) {
      throw new Error("返回数据格式不对");
    }
    state.data = data;
    state.cid = data.selected ? data.selected.cid : (data.sessions[0] || {}).cid || null;
    el("error").hidden = true;
    render();
  } catch (error) {
    el("error").hidden = false;
    el("error").textContent = `读取失败：${(error && error.message) || error}`;
    if (!state.data) el("empty").textContent = "读取失败，请确认插件已启用后重试。";
  } finally {
    state.loading = false;
  }
}

async function loadStats(days = state.days) {
  if (!bridge) return;
  if (state.statsLoading) return;
  state.statsLoading = true;
  try {
    const raw = await bridge.apiGet("stats", { days, basis: state.basis });
    const data = raw && typeof raw === "object" && "series" in raw ? raw : raw && raw.data;
    if (!data || !Array.isArray(data.series)) throw new Error("返回数据格式不对");
    state.stats = data;
    state.days = data.days || days;
    state.basis = data.basis || state.basis;
    el("basis").value = state.basis;
    const select = el("range");
    if (!Array.from(select.options).some((option) => Number(option.value) === state.days)) {
      const option = document.createElement("option");
      option.value = String(state.days);
      option.textContent = `近 ${state.days} 天`;
      select.append(option);
    }
    select.value = String(state.days);
    el("error").hidden = true;
    renderStats();
  } catch (error) {
    el("error").hidden = false;
    el("error").textContent = `读取失败：${(error && error.message) || error}`;
  } finally {
    state.statsLoading = false;
  }
}

function render() {
  const data = state.data;
  if (!data) return;
  const prices = data.prices || {};
  const dbPath = String(data.db || "");
  const dbName = dbPath.substring(Math.max(dbPath.lastIndexOf("\\"), dbPath.lastIndexOf("/")) + 1) || dbPath;
  el("subtitle").textContent =
    `单价 未命中 ${formatPrice(prices.miss)} · 命中 ${formatPrice(prices.hit)} · `
    + `输出 ${formatPrice(prices.out)} 每百万 · ${dbName}`;
  el("subtitle").title = `数据库：${data.db || ""}`;
  el("updated").textContent = data.now ? `更新于 ${data.now.slice(11)}` : "";
  el("count").textContent = `${data.total} 个`;
  renderSummary(data.summary || {});
  renderList();
  renderDetail();
}

function select(cid) {
  if (!cid || cid === state.cid) return;
  state.cid = cid;
  load(cid);
}

function switchTab(tab) {
  state.tab = tab;
  const sessions = tab === "sessions";
  el("tab-sessions").hidden = !sessions;
  el("tab-stats").hidden = sessions;
  el("tab-sessions-btn").classList.toggle("active", sessions);
  el("tab-stats-btn").classList.toggle("active", !sessions);
  el("tab-sessions-btn").setAttribute("aria-selected", String(sessions));
  el("tab-stats-btn").setAttribute("aria-selected", String(!sessions));
  if (!sessions) loadStats();
}

function syncTimer() {
  if (state.timer) {
    clearInterval(state.timer);
    state.timer = null;
  }
  if (el("auto").checked) {
    state.timer = setInterval(() => {
      if (document.hidden) return;
      if (state.tab === "sessions") load(state.cid);
      else loadStats();
    }, 15000);
  }
}

/* ---------------------------------------------------------------- 启动 */

el("refresh").addEventListener("click", () => {
  if (state.tab === "sessions") load(state.cid);
  else loadStats();
});
el("auto").addEventListener("change", syncTimer);
el("search").addEventListener("input", (event) => {
  state.query = event.target.value;
  if (state.data) renderList();
});
el("tab-sessions-btn").addEventListener("click", () => switchTab("sessions"));
el("tab-stats-btn").addEventListener("click", () => switchTab("stats"));
el("range").addEventListener("change", (event) => loadStats(Number(event.target.value)));
el("basis").addEventListener("change", (event) => {
  state.basis = event.target.value === "list" ? "list" : "official";
  if (state.tab === "sessions") load(state.cid);
  else loadStats();
});
el("export").addEventListener("click", exportCsv);

document.title = "看上下文";

if (!bridge) {
  el("empty").textContent = "页面桥接没有加载成功，请从 AstrBot 面板重新打开这个页面。";
} else {
  try {
    await bridge.ready();
  } catch (error) {
    console.warn("bridge.ready failed", error);
  }
  await load();
}
