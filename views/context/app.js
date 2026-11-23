const bridge = window.AstrBotPluginView;
const el = (id) => document.getElementById(id);

const state = {
  data: null,
  cid: null,
  query: "",
  timer: null,
  loading: false,
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

function cell(text, className) {
  const td = document.createElement("td");
  if (className) td.className = className;
  td.textContent = text;
  return td;
}

/* ---------------------------------------------------------------- 渲染 */

function render() {
  const data = state.data;
  if (!data) return;
  const prices = data.prices || {};
  const dbName = String(data.db || "").split(/[\\/]/).pop() || data.db;
  const subtitle =
    `单价 未命中 ${formatPrice(prices.miss)} · 命中 ${formatPrice(prices.hit)} · `
    + `输出 ${formatPrice(prices.out)} 每百万 · 数据源 ${dbName}`;
  el("subtitle").textContent = subtitle;
  el("subtitle").title = `数据库：${data.db}`;
  el("updated").textContent = data.now ? `更新于 ${data.now.slice(11)}` : "";
  el("count").textContent = `${data.total} 个`;
  renderList();
  renderDetail();
}

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

  const name = document.createElement("span");
  name.className = "session-name";
  name.textContent = item.label;
  name.title = item.label;

  const sub = document.createElement("span");
  sub.className = "session-sub";
  sub.textContent = `${item.messages.toLocaleString("zh-CN")} 条消息 · ${item.updated || "—"}`;

  const num = document.createElement("span");
  num.className = "session-num";
  num.textContent = item.has_stats
    ? formatTokens(item.ctx)
    : `≈${formatTokens(item.estimated_ctx)}`;

  const rate = document.createElement("span");
  rate.className = "session-rate";
  rate.textContent = item.has_stats ? `命中 ${formatRate(item.rate)}` : "无请求记录";

  row.append(name, sub, num, rate);
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
  el("capacity").textContent = capacity;
  renderRecent(selected.recent || []);
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

/* ---------------------------------------------------------------- 取数 */

async function load(cid = state.cid) {
  if (!bridge) return;
  if (state.loading) return;
  state.loading = true;
  if (!state.data) el("empty").textContent = "正在读取…";
  try {
    const raw = await bridge.apiGet("overview", { cid: cid || "", recent: 10 });
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

function select(cid) {
  if (!cid || cid === state.cid) return;
  state.cid = cid;
  load(cid);
}

function syncTimer() {
  if (state.timer) {
    clearInterval(state.timer);
    state.timer = null;
  }
  if (el("auto").checked) {
    state.timer = setInterval(() => {
      if (!document.hidden) load(state.cid);
    }, 15000);
  }
}

/* ---------------------------------------------------------------- 启动 */

el("refresh").addEventListener("click", () => load(state.cid));
el("auto").addEventListener("change", syncTimer);
el("search").addEventListener("input", (event) => {
  state.query = event.target.value;
  if (state.data) renderList();
});

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
