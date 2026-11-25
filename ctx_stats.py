"""「看上下文」的统计层：把 provider_stats 全表聚合成日 / 模型 / 渠道 / 小时维度。

ctx_core 负责「某个会话现在多大」，这里负责「这些天一共花了多少、花在哪」。
纯标准库，不 import astrbot；插件、网页视图和命令行共用。

口径：
  - created_at 存的是 UTC，统一换成东八区再按「本地日」分桶；
  - 每条请求的花费 = 未命中 × miss 单价 + 命中 × hit 单价 + 输出 × out 单价；
  - 缓存省下的钱 = 把命中的 token 按未命中价重算后的差额；
  - 谷时折扣（可选，默认关）按请求发生时刻判断，不按当前时刻。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

try:  # 插件以包的方式加载
    from . import ctx_core
except ImportError:  # pragma: no cover - 命令行/兜底
    import ctx_core  # type: ignore[no-redef]

_KIND_LABEL = {
    "FriendMessage": "私聊",
    "GroupMessage": "群聊",
    "GuildMessage": "频道",
}

_OK_STATUS = {"completed", "success", "ok", ""}
_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


# ---------------------------------------------------------------- 价格


@dataclass
class PriceBook:
    """默认单价 + 按模型覆盖。单价单位是「元 / token」。"""

    default: ctx_core.Prices = field(default_factory=ctx_core.Prices)
    overrides: dict[str, ctx_core.Prices] = field(default_factory=dict)

    def for_model(self, model: str) -> ctx_core.Prices:
        key = (model or "").strip().lower()
        if not key:
            return self.default
        if key in self.overrides:
            return self.overrides[key]
        leaf = key.split("/")[-1]
        return self.overrides.get(leaf, self.default)


def parse_price_book(default: ctx_core.Prices, raw: object) -> PriceBook:
    """把配置里的 JSON 文本解析成按模型的价格表。

    形如 {"deepseek-reasoner": {"miss": 4, "hit": 0.04, "out": 16}}，
    数字单位是「元 / 百万 token」，与配置项一致。
    """
    book = PriceBook(default=default)
    if not raw:
        return book
    text = str(raw).strip()
    if not text:
        return book
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return book
    if not isinstance(data, dict):
        return book
    for model, value in data.items():
        if not isinstance(value, dict):
            continue
        try:
            miss = float(value.get("miss", default.miss * 1_000_000))
            hit = float(value.get("hit", default.hit * 1_000_000))
            out = float(value.get("out", default.out * 1_000_000))
        except (TypeError, ValueError):
            continue
        book.overrides[str(model).strip().lower()] = ctx_core.Prices(
            miss=miss / 1_000_000,
            hit=hit / 1_000_000,
            out=out / 1_000_000,
        )
    return book


# ---------------------------------------------------------------- 峰谷


@dataclass(frozen=True)
class OffPeak:
    """DeepSeek 官方 API 的峰谷计价。

    官方口径：北京时间周一至周五（不含法定节假日）的 09:00–12:00、14:00–18:00
    是峰时，按原价；其余所有时间（含两个午休间隙、夜间、周末和节假日全天）是谷时，
    按 discount 打折（默认五折）。配置里的默认单价就是峰时价。

    holidays 里的日期整天按谷时算；workdays 里的日期（调休上班）按工作日算，
    用来覆盖落在周末的补班日。峰时窗口只支持同一天内的区间（start < end）。
    """

    enabled: bool = False
    windows: tuple[tuple[str, str], ...] = (("09:00", "12:00"), ("14:00", "18:00"))
    weekdays: tuple[int, ...] = (1, 2, 3, 4, 5)  # 周一 1 … 周日 7
    holidays: tuple[str, ...] = ()
    workdays: tuple[str, ...] = ()
    discount: float = 0.5

    def ranges(self) -> list[tuple[int, int]]:
        result = []
        for start_text, end_text in self.windows:
            start, end = _minutes(start_text), _minutes(end_text)
            if start is None or end is None or start >= end:
                continue
            result.append((start, end))
        return result

    def is_workday(self, day) -> bool:
        key = day.isoformat()
        if key in self.workdays:
            return True
        if key in self.holidays:
            return False
        return day.isoweekday() in self.weekdays

    def is_peak(self, at: datetime | None) -> bool:
        """峰时 = 工作日 + 落在任一峰时窗口内。"""
        if at is None or not self.is_workday(at.date()):
            return False
        minute = at.hour * 60 + at.minute
        return any(start <= minute < end for start, end in self.ranges())

    def factor(self, at: datetime | None) -> float:
        if not self.enabled or at is None:
            return 1.0
        if self.is_peak(at):
            return 1.0
        return max(0.0, min(1.0, float(self.discount)))

    def next_change(self, at: datetime) -> int | None:
        """离下一次峰↔谷切换还有几分钟（最多往后找 8 天）。"""
        base = at.replace(second=0, microsecond=0)
        current = self.is_peak(base)
        for step in range(1, 8 * 24 * 60 + 1):
            if self.is_peak(base + timedelta(minutes=step)) != current:
                return step
        return None

    def describe(self) -> str:
        windows = "、".join(f"{start}–{end}" for start, end in self.windows)
        return f"工作日 {windows}"


def _minutes(text: str) -> int | None:
    try:
        hour, minute = str(text).strip().split(":")
        hour, minute = int(hour), int(minute)
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


_DEFAULT_WINDOWS: tuple[tuple[str, str], ...] = (("09:00", "12:00"), ("14:00", "18:00"))
_DEFAULT_WEEKDAYS: tuple[int, ...] = (1, 2, 3, 4, 5)
_WEEKDAY_NAMES = {
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
    "sun": 7,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "日": 7,
    "天": 7,
}


def _parse_windows(raw: object) -> tuple[tuple[str, str], ...]:
    """把配置里的峰时窗口解析成 ((start, end), ...)。

    接受 JSON 二维数组 [["09:00","12:00"], ["14:00","18:00"]]，
    也接受 "09:00-12:00, 14:00-18:00" 这种简写；解析失败回退默认窗口。
    """
    text = str(raw or "").strip()
    if not text:
        return _DEFAULT_WINDOWS
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        data = text.replace("，", ",").split(",")
    items = data if isinstance(data, list) else []
    windows = []
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            start, end = item
        else:
            pieces = str(item).replace("–", "-").replace("—", "-").split("-")
            if len(pieces) != 2:
                continue
            start, end = pieces
        start, end = str(start).strip(), str(end).strip()
        if _minutes(start) is None or _minutes(end) is None:
            continue
        windows.append((start, end))
    return tuple(windows) or _DEFAULT_WINDOWS


def _weekday_number(text: str) -> int | None:
    text = text.strip().lower()
    if text.isdigit() and 1 <= int(text) <= 7:
        return int(text)
    return _WEEKDAY_NAMES.get(text)


def _parse_weekdays(raw: object) -> tuple[int, ...]:
    """接受 "1-5" / "1,2,3" / "mon-fri"，1=周一 … 7=周日。"""
    text = str(raw or "").strip().lower()
    if not text:
        return _DEFAULT_WEEKDAYS
    values: list[int] = []
    for part in text.replace("，", ",").replace("、", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            first, last = _weekday_number(left), _weekday_number(right)
            if first and last and first <= last:
                values.extend(range(first, last + 1))
            continue
        number = _weekday_number(part)
        if number:
            values.append(number)
    return tuple(sorted(set(values))) or _DEFAULT_WEEKDAYS


def _parse_dates(raw: object) -> tuple[str, ...]:
    """逗号分隔的 YYYY-MM-DD 日期列表，非法项忽略。"""
    text = str(raw or "").strip()
    if not text:
        return ()
    dates = []
    for part in text.replace("，", ",").replace("、", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            dates.append(datetime.strptime(part, "%Y-%m-%d").date().isoformat())
        except ValueError:
            continue
    return tuple(sorted(set(dates)))


def build_offpeak(
    enabled: bool = False,
    windows: object = "",
    weekdays: object = "",
    holidays: object = "",
    workdays: object = "",
    discount: float = 0.5,
) -> OffPeak:
    """从插件配置的原始文本构造 OffPeak，解析失败一律回退默认口径。"""
    return OffPeak(
        enabled=bool(enabled),
        windows=_parse_windows(windows),
        weekdays=_parse_weekdays(weekdays),
        holidays=_parse_dates(holidays),
        workdays=_parse_dates(workdays),
        discount=float(discount),
    )


_BASIS_LABEL = {"official": "官方 API 口径", "list": "原价口径"}


def basis_label(basis: str) -> str:
    return _BASIS_LABEL.get(str(basis or "").strip().lower(), _BASIS_LABEL["official"])


def normalize_basis(value: object) -> str:
    text = str(value or "").strip().lower()
    return "list" if text in {"list", "price", "full", "orig", "原价"} else "official"


def peak_state(now: datetime, off: OffPeak) -> dict:
    """当前处在峰时还是谷时，以及还有多久切换。"""
    if not off.enabled:
        return {"enabled": False}
    active = off.is_peak(now)
    remain = off.next_change(now)
    if remain is None:
        until = "—"
    elif remain >= 60:
        until = f"{remain // 60}h{remain % 60:02d}m"
    else:
        until = f"{remain}m"
    return {
        "enabled": True,
        "active": active,
        "label": "峰时" if active else "谷时",
        "until": until,
        "until_minutes": remain,
        "discount": float(off.discount),
        "windows": "、".join(f"{start}–{end}" for start, end in off.windows),
        "schedule": off.describe(),
    }


# ---------------------------------------------------------------- 数据结构


@dataclass
class Request:
    at: datetime | None  # 本地时间
    miss: int
    hit: int
    out: int
    model: str
    umo: str
    status: str
    seconds: float
    cid: str = ""

    @property
    def ctx(self) -> int:
        return self.miss + self.hit

    @property
    def ok(self) -> bool:
        return (self.status or "").strip().lower() in _OK_STATUS


def _kind_of(umo: str) -> str:
    bits = (umo or "").split(":")
    raw = bits[1] if len(bits) > 1 else ""
    return _KIND_LABEL.get(raw, raw or "其他")


def _seconds(start: object, end: object) -> float:
    try:
        value = float(end) - float(start)
    except (TypeError, ValueError):
        return 0.0
    return value if 0 < value < 3600 else 0.0


def load_requests(con: sqlite3.Connection) -> list[Request]:
    store_local = ctx_core._store_is_local(con)
    rows = con.execute(
        "SELECT created_at, token_input_other, token_input_cached, token_output, "
        "       provider_model, umo, status, start_time, end_time, conversation_id "
        "FROM provider_stats ORDER BY id"
    ).fetchall()
    return [
        Request(
            at=ctx_core._local(ctx_core._parse_dt(row[0]), store_local),
            miss=int(row[1] or 0),
            hit=int(row[2] or 0),
            out=int(row[3] or 0),
            model=str(row[4] or ""),
            umo=str(row[5] or ""),
            status=str(row[6] or ""),
            seconds=_seconds(row[7], row[8]),
            cid=str(row[9] or ""),
        )
        for row in rows
    ]


# ---------------------------------------------------------------- 聚合


def _blank() -> dict:
    return {
        "requests": 0,
        "miss": 0,
        "hit": 0,
        "out": 0,
        "ctx": 0,
        "cost": 0.0,
        "gross": 0.0,
        "nocache": 0.0,
        "saved": 0.0,
        "fail": 0,
        "seconds": 0.0,
        "lat_n": 0,
    }


def _add(bucket: dict, req: Request, book: PriceBook, off: OffPeak) -> None:
    price = book.for_model(req.model)
    factor = off.factor(req.at)
    raw = req.miss * price.miss + req.hit * price.hit + req.out * price.out
    cost = raw * factor
    nocache = (req.ctx * price.miss + req.out * price.out) * factor
    bucket["requests"] += 1
    bucket["miss"] += req.miss
    bucket["hit"] += req.hit
    bucket["out"] += req.out
    bucket["ctx"] += req.ctx
    bucket["cost"] += cost
    bucket["gross"] += raw
    bucket["nocache"] += nocache
    bucket["saved"] += nocache - cost
    bucket["seconds"] += req.seconds
    if req.seconds > 0:
        bucket["lat_n"] += 1
    if not req.ok:
        bucket["fail"] += 1


def _finish(bucket: dict) -> dict:
    ctx = bucket["ctx"]
    return {
        "requests": bucket["requests"],
        "miss": bucket["miss"],
        "hit": bucket["hit"],
        "out": bucket["out"],
        "ctx": ctx,
        "cost": round(bucket["cost"], 6),
        "gross": round(bucket["gross"], 6),
        "peak_saved": round(bucket["gross"] - bucket["cost"], 6),
        "nocache": round(bucket["nocache"], 6),
        "saved": round(bucket["saved"], 6),
        "rate": round(bucket["hit"] / ctx * 100, 1) if ctx else 0.0,
        "fail": bucket["fail"],
        "avg_seconds": round(bucket["seconds"] / bucket["lat_n"], 2) if bucket["lat_n"] else 0.0,
        "lat_n": bucket["lat_n"],
    }


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def _label_day(day: datetime, now: datetime) -> str:
    if day.date() == now.date():
        return "今天"
    return f"{day.month}-{day.day}"


def _day_series(
    by_day: dict[str, dict],
    days: int,
    now: datetime,
    prices_note: bool = False,
) -> list[dict]:
    series = []
    for offset in range(max(1, days) - 1, -1, -1):
        day = (now - timedelta(days=offset)).date()
        key = day.isoformat()
        raw = by_day.get(key)
        item = _finish(raw) if raw else _finish(_blank())
        series.append(
            {
                "day": key,
                "label": f"{day.month}-{day.day}",
                "weekday": _WEEKDAYS[day.weekday()],
                **item,
            }
        )
    return series


def compute(
    db_path,
    *,
    days: int = 14,
    prices: ctx_core.Prices | None = None,
    book: PriceBook | None = None,
    off: OffPeak | None = None,
    basis: str = "official",
    daily_budget: float = 0.0,
    monthly_budget: float = 0.0,
    now: datetime | None = None,
    limit_failures: int = 12,
) -> dict:
    """一次读全表，返回网页/聊天都能用的统计 dict。"""
    prices = prices or ctx_core.Prices()
    book = book or PriceBook(default=prices)
    off = off or OffPeak()
    basis = normalize_basis(basis)
    if basis == "list":
        # 原价口径：不套谷时折扣，当上限估算用
        off = OffPeak(enabled=False)
    now = now or datetime.now()

    con = ctx_core.connect(db_path)
    try:
        requests = load_requests(con)
    finally:
        con.close()

    all_b = _blank()
    today_b = _blank()
    week_b = _blank()
    month_b = _blank()
    window_b = _blank()
    by_day: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_kind: dict[str, dict] = {}
    by_hour: list[dict] = [_blank() for _ in range(24)]
    by_session: dict[str, dict] = {}
    failures: list[dict] = []
    latencies: list[float] = []

    week_start = (now - timedelta(days=6)).date()
    days_window = max(1, int(days))
    window_start = (now - timedelta(days=days_window - 1)).date()

    for req in requests:
        _add(all_b, req, book, off)
        key = req.at.date().isoformat() if req.at else ""
        if req.at:
            bucket = by_day.setdefault(key, _blank())
            _add(bucket, req, book, off)
        if req.at and req.at.date() == now.date():
            _add(today_b, req, book, off)
        if req.at and week_start <= req.at.date() <= now.date():
            _add(week_b, req, book, off)
        if req.at and req.at.year == now.year and req.at.month == now.month:
            _add(month_b, req, book, off)
        if req.at and window_start <= req.at.date() <= now.date():
            _add(window_b, req, book, off)

        in_window = bool(req.at and window_start <= req.at.date() <= now.date())
        if in_window:
            model = req.model or "未记录"
            _add(by_model.setdefault(model, _blank()), req, book, off)
            _add(by_kind.setdefault(_kind_of(req.umo), _blank()), req, book, off)
            _add(by_hour[req.at.hour], req, book, off)

        cid = getattr(req, "cid", "") or ""
        if cid:
            bucket = by_session.setdefault(
                cid, {"requests": 0, "cost": 0.0, "hit": 0, "miss": 0, "last": None}
            )
            bucket["requests"] += 1
            bucket["hit"] += req.hit
            bucket["miss"] += req.miss
            if req.at and (bucket["last"] is None or req.at > bucket["last"]):
                bucket["last"] = req.at
            price = book.for_model(req.model)
            factor = off.factor(req.at)
            bucket["cost"] += (
                req.miss * price.miss + req.hit * price.hit + req.out * price.out
            ) * factor

        if in_window and req.seconds > 0:
            latencies.append(req.seconds)
        if in_window and not req.ok:
            failures.append(
                (
                    req.at,
                    {
                        "at": ctx_core._fmt_time(req.at),
                        "model": req.model or "未记录",
                        "status": req.status or "?",
                        "kind": _kind_of(req.umo),
                    },
                )
            )

    failures.sort(key=lambda pair: pair[0] or datetime.min, reverse=True)
    recent_failures = [item for _, item in failures[: max(0, int(limit_failures))]]

    session_payload = {}
    for cid, item in by_session.items():
        ctx = item["hit"] + item["miss"]
        session_payload[cid] = {
            "requests_total": item["requests"],
            "cost_total": round(item["cost"], 6),
            "rate_total": round(item["hit"] / ctx * 100, 1) if ctx else 0.0,
            "last_at": ctx_core._fmt_time(item["last"]),
        }

    series = _day_series(by_day, days_window, now)
    peak_cost = sum(item["cost"] for item in series)
    models = sorted(
        ({"model": name, **_finish(item)} for name, item in by_model.items()),
        key=lambda item: (-item["cost"], -item["requests"]),
    )
    for item in models:
        item["cost"] = round(item["cost"], 6)
        item["nocache"] = round(item["nocache"], 6)
        item["saved"] = round(item["saved"], 6)
    total_cost = sum(item["cost"] for item in models) or 1.0
    for item in models:
        item["share"] = round(item["cost"] / total_cost * 100, 1)

    kinds = sorted(
        ({"kind": name, **_finish(item)} for name, item in by_kind.items()),
        key=lambda item: -item["cost"],
    )
    for item in kinds:
        item["cost"] = round(item["cost"], 6)
        item["share"] = round(item["cost"] / total_cost * 100, 1)

    hours = []
    for hour, bucket in enumerate(by_hour):
        item = _finish(bucket)
        hours.append({"hour": hour, "requests": item["requests"], "cost": item["cost"], "ctx": item["ctx"]})

    daily_used = _finish(today_b)["cost"]
    month_used = _finish(month_b)["cost"]
    budget = {
        "daily": round(float(daily_budget or 0), 4),
        "daily_used": daily_used,
        "daily_rate": round(daily_used / daily_budget * 100, 1) if daily_budget else 0.0,
        "monthly": round(float(monthly_budget or 0), 4),
        "monthly_used": month_used,
        "monthly_rate": round(month_used / monthly_budget * 100, 1) if monthly_budget else 0.0,
    }

    return {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "days": days_window,
        "basis": basis,
        "basis_label": basis_label(basis),
        "peak": peak_state(now, off),
        "budget": budget,
        "all": _finish(all_b),
        "today": _finish(today_b),
        "week": _finish(week_b),
        "month": _finish(month_b),
        "window": {**_finish(window_b), "cost_series": round(peak_cost, 6)},
        "series": series,
        "by_model": models,
        "by_kind": kinds,
        "by_hour": hours,
        "failures": recent_failures,
        "latency": {
            "avg": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
            "p50": round(_percentile(latencies, 0.5), 2),
            "p95": round(_percentile(latencies, 0.95), 2),
            "n": len(latencies),
        },
        "sessions": session_payload,
        "prices": {
            "miss": prices.miss * 1_000_000,
            "hit": prices.hit * 1_000_000,
            "out": prices.out * 1_000_000,
        },
    }


# ---------------------------------------------------------------- 聊天文本


def _peak_note(bucket: dict, short: bool = False) -> str:
    """有峰谷折扣时补一句：原价多少、谷时省了多少。"""
    saved = float(bucket.get("peak_saved") or 0.0)
    if saved <= 0.000001:
        return ""
    if short:
        return f"（谷时省 {ctx_core._money(saved)}）"
    return f"（原价 {ctx_core._money(bucket.get('gross', 0))}，谷时省 {ctx_core._money(saved)}）"


def _brief(summary: dict) -> str:
    today = summary.get("today", {})
    week = summary.get("week", {})
    month = summary.get("month", {})
    all_time = summary.get("all", {})
    line = (
        f"今日 {ctx_core._money(today.get('cost', 0))}{_peak_note(today)}（{today.get('requests', 0)} 次）"
        f" · 本周 {ctx_core._money(week.get('cost', 0))}"
        f" · 本月 {ctx_core._money(month.get('cost', 0))}"
        f" · 累计 {ctx_core._money(all_time.get('cost', 0))}"
    )
    budget = summary.get("budget") or {}
    if budget.get("daily"):
        line += f"\n日预算 {ctx_core._money(budget['daily_used'])} / {ctx_core._money(budget['daily'])}"
        line += f"（{budget['daily_rate']:.0f}%）"
    if budget.get("monthly"):
        line += f" · 月预算 {ctx_core._money(budget['monthly_used'])} / {ctx_core._money(budget['monthly'])}"
        line += f"（{budget['monthly_rate']:.0f}%）"
    peak = summary.get("peak") or {}
    if peak.get("enabled"):
        line += f"\n当前{peak['label']}，{peak['until']} 后切换（谷时 {peak['discount'] * 10:g} 折）"
    return line


def render_brief(summary: dict) -> str:
    """当前会话命令末尾那一小段：今日 / 本周 / 本月 / 累计 + 预算。"""
    return _brief(summary)


def render_stats(summary: dict, days: int) -> str:
    window = summary.get("window", {})
    all_time = summary.get("all", {})
    span = max(1, int(days))
    lines = [
        (
            "【费用统计】今天" if span == 1 else f"【费用统计】近 {span} 天"
        ) + f" · {basis_label(summary.get('basis', 'official'))}",
        (
            f"请求 {ctx_core._n(window.get('requests'))} 次"
            f" · 上下文 {ctx_core._n(window.get('ctx'))}"
            f" · 输出 {ctx_core._n(window.get('out'))}"
            f" · 缓存命中 {ctx_core._pct(window.get('hit', 0), window.get('ctx', 0))}"
        ),
        (
            f"花费 {ctx_core._money(window.get('cost', 0))}{_peak_note(window, short=True)}"
            f" · 缓存省下 {ctx_core._money(window.get('saved', 0))}"
            f" · 全未命中约 {ctx_core._money(window.get('nocache', 0))}"
        ),
    ]
    models = summary.get("by_model") or []
    if models:
        top = models[:3]
        lines.append(
            "模型 "
            + " · ".join(
                f"{item['model']} {ctx_core._n(item['requests'])} 次 {ctx_core._money(item['cost'])}"
                for item in top
            )
        )
    latency = summary.get("latency") or {}
    if latency.get("n"):
        lines.append(
            f"延迟 平均 {latency['avg']:.1f}s · P50 {latency['p50']:.1f}s · P95 {latency['p95']:.1f}s"
        )
    if window.get("fail"):
        lines.append(f"失败 {ctx_core._n(window.get('fail'))} 次")
    lines.append(_brief(summary))
    lines.append(f"累计共 {ctx_core._n(all_time.get('requests'))} 次请求、{ctx_core._money(all_time.get('cost', 0))}")
    return "\n".join(lines)


def render_help_extra() -> str:
    return (
        "/上下文今日      今天花了多少、几个请求\n"
        "/上下文统计      最近 stats_days 天（默认 14）的汇总\n"
    )
