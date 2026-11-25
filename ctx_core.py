"""「看上下文」核心逻辑：读 AstrBot 的 data_v4.db，算上下文大小、缓存命中与花费。

纯标准库实现，不 import astrbot，插件与命令行脚本共用同一份逻辑。

AstrBot 4.30 起会话改成事件溯源存储，旧的 conversations 表没了：
  conversations_v3     会话元数据（conversation_id / umo / title / updated_at）
  conversation_events  事件流（message.appended / context.rebased …）
  provider_stats       每次请求的 token 明细（token_input_other / token_input_cached / token_output）

于是：
  当前上下文 token = 该会话最新一条 provider_stats 的 未命中 + 命中
  消息条数         = 最近一次 context.rebased 快照长度 + 之后新增的 message.appended
"""

from __future__ import annotations

import glob
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

DB_FILENAME = "data_v4.db"

# 2026-11-02 起的价格：未命中输入 / 命中输入 / 输出（元每 token）
PRICE_MISS = 2.0 / 1_000_000
PRICE_HIT = 0.04 / 1_000_000
PRICE_OUT = 8.0 / 1_000_000

LOCAL_OFFSET = timedelta(hours=8)

_KIND_LABEL = {
    "FriendMessage": "私聊",
    "GroupMessage": "群聊",
    "GuildMessage": "频道",
}


# ---------------------------------------------------------------- 价格与格式


@dataclass(frozen=True)
class Prices:
    miss: float = PRICE_MISS
    hit: float = PRICE_HIT
    out: float = PRICE_OUT

    def cost(self, miss: int, hit: int, out: int) -> float:
        return miss * self.miss + hit * self.hit + out * self.out


def _n(value: float | int | None) -> str:
    """千分位整数。"""
    return f"{int(value or 0):,}"


def _money(value: float) -> str:
    if value <= 0:
        return "¥0"
    if value < 0.01:
        return f"¥{value:.4f}"
    if value < 1:
        return f"¥{value:.3f}".rstrip("0").rstrip(".")
    return f"¥{value:.2f}"


def _pct(part: float, whole: float) -> str:
    if not whole:
        return "0%"
    return f"{part / whole * 100:.1f}%"


# ---------------------------------------------------------------- 时间处理


def _parse_dt(value: object) -> datetime | None:
    """把库里的时间字符串解析成 naive datetime（先不判断时区）。"""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    text = text.split("+")[0].rstrip("Z")
    for fmt, size in (
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%d %H:%M", 16),
        ("%Y-%m-%d", 10),
    ):
        try:
            return datetime.strptime(text[:size], fmt)
        except ValueError:
            continue
    return None


def _store_is_local(con: sqlite3.Connection) -> bool:
    """AstrBot 的 created_at 存 UTC；万一某个版本改成存本地时间，用最新一条判断。"""
    try:
        row = con.execute("SELECT MAX(created_at) FROM provider_stats").fetchone()
    except sqlite3.Error:
        return False
    latest = _parse_dt(row[0] if row else None)
    if latest is None:
        return False
    # 若把本地时间当成 UTC 看，会比“现在”晚 8 小时，这里用 1 小时做分界
    return latest - datetime.utcnow() > timedelta(hours=1)


def _local(dt: datetime | None, store_local: bool) -> datetime | None:
    if dt is None:
        return None
    return dt if store_local else dt + LOCAL_OFFSET


def _fmt_time(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "—"
    now = now or datetime.now()
    if dt.year == now.year:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- 数据结构


@dataclass
class Stat:
    """一次请求的 token 明细。"""

    at: datetime | None
    miss: int
    hit: int
    out: int
    model: str = ""

    @property
    def ctx(self) -> int:
        return self.miss + self.hit

    @property
    def total(self) -> int:
        return self.ctx + self.out

    @property
    def rate(self) -> float:
        return self.hit / self.ctx if self.ctx else 0.0

    def cost(self, prices: Prices) -> float:
        return prices.cost(self.miss, self.hit, self.out)


@dataclass
class Session:
    cid: str
    umo: str
    title: str
    updated: datetime | None
    messages: int
    chars: int
    last: Stat | None = None


# ---------------------------------------------------------------- 读数据库


# ---------------------------------------------------------------- 找数据库


def find_db() -> str:
    """按可靠程度依次尝试：AstrBot 自己的路径 → launcher 实例目录 → 当前目录。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        candidate = Path(get_astrbot_data_path()) / DB_FILENAME
        if candidate.is_file():
            return str(candidate)
    except Exception:  # noqa: BLE001 - 命令行里没有 astrbot 就算了
        pass

    patterns = [
        str(Path.home() / ".astrbot_launcher" / "instances" / "*" / "core" / "data" / DB_FILENAME),
        str(Path.cwd() / "data" / DB_FILENAME),
        str(Path.cwd() / "core" / "data" / DB_FILENAME),
    ]
    found: list[str] = []
    for pattern in patterns:
        found.extend(glob.glob(pattern))
    if not found:
        return ""
    return max(found, key=os.path.getmtime)


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"找不到数据库：{path}")
    try:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
        con.execute("SELECT 1 FROM conversations_v3 LIMIT 1").fetchone()
    except sqlite3.Error:
        con = sqlite3.connect(str(path.resolve()), timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


_SESSIONS_SQL = """
SELECT
    c.conversation_id                                   AS cid,
    c.umo                                               AS umo,
    c.title                                             AS title,
    c.updated_at                                        AS updated_at,
    COALESCE((
        SELECT json_array_length(r.payload, '$.messages')
        FROM conversation_events r
        WHERE r.conversation_ref = c.id AND r.type = 'context.rebased'
        ORDER BY r.seq DESC LIMIT 1
    ), 0)                                               AS base_msgs,
    COALESCE((
        SELECT length(r.payload)
        FROM conversation_events r
        WHERE r.conversation_ref = c.id AND r.type = 'context.rebased'
        ORDER BY r.seq DESC LIMIT 1
    ), 0)                                               AS base_chars,
    (
        SELECT COUNT(*) FROM conversation_events e
        WHERE e.conversation_ref = c.id AND e.type = 'message.appended'
          AND e.seq > COALESCE((
              SELECT MAX(r2.seq) FROM conversation_events r2
              WHERE r2.conversation_ref = c.id AND r2.type = 'context.rebased'
          ), -1)
    )                                                   AS new_msgs,
    COALESCE((
        SELECT SUM(length(e.payload)) FROM conversation_events e
        WHERE e.conversation_ref = c.id AND e.type = 'message.appended'
          AND e.seq > COALESCE((
              SELECT MAX(r2.seq) FROM conversation_events r2
              WHERE r2.conversation_ref = c.id AND r2.type = 'context.rebased'
          ), -1)
    ), 0)                                               AS new_chars
FROM conversations_v3 c
ORDER BY c.updated_at DESC
"""

_LATEST_STATS_SQL = """
SELECT s.conversation_id      AS cid,
       s.created_at           AS created_at,
       s.token_input_other    AS miss,
       s.token_input_cached   AS hit,
       s.token_output         AS out,
       s.provider_model       AS model
FROM provider_stats s
JOIN (
    SELECT conversation_id AS cid, MAX(id) AS mid
    FROM provider_stats
    WHERE conversation_id IS NOT NULL AND conversation_id <> ''
    GROUP BY conversation_id
) m ON m.mid = s.id
"""


def fetch_sessions(con: sqlite3.Connection, store_local: bool | None = None) -> list[Session]:
    if store_local is None:
        store_local = _store_is_local(con)
    latest: dict[str, Stat] = {}
    for row in con.execute(_LATEST_STATS_SQL):
        latest[str(row["cid"])] = Stat(
            at=_local(_parse_dt(row["created_at"]), store_local),
            miss=int(row["miss"] or 0),
            hit=int(row["hit"] or 0),
            out=int(row["out"] or 0),
            model=str(row["model"] or ""),
        )

    sessions: list[Session] = []
    for row in con.execute(_SESSIONS_SQL):
        cid = str(row["cid"] or "")
        sessions.append(
            Session(
                cid=cid,
                umo=str(row["umo"] or ""),
                title=str(row["title"] or ""),
                updated=_local(_parse_dt(row["updated_at"]), store_local),
                messages=int(row["base_msgs"] or 0) + int(row["new_msgs"] or 0),
                chars=int(row["base_chars"] or 0) + int(row["new_chars"] or 0),
                last=latest.get(cid),
            )
        )
    return sessions


def fetch_recent(
    con: sqlite3.Connection, cid: str, limit: int = 3, store_local: bool | None = None
) -> list[Stat]:
    if store_local is None:
        store_local = _store_is_local(con)
    rows = con.execute(
        "SELECT created_at, token_input_other, token_input_cached, token_output, provider_model "
        "FROM provider_stats WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
        (cid, max(1, int(limit))),
    ).fetchall()
    return [
        Stat(
            at=_local(_parse_dt(row["created_at"]), store_local),
            miss=int(row["token_input_other"] or 0),
            hit=int(row["token_input_cached"] or 0),
            out=int(row["token_output"] or 0),
            model=str(row["provider_model"] or ""),
        )
        for row in rows
    ]


def pick_session(sessions: list[Session], cid: str | None, umo: str | None) -> Session | None:
    """优先按当前会话 id 找；找不到就退到该 umo 最近一次活动。"""
    if cid:
        for s in sessions:
            if s.cid == cid:
                return s
    if umo:
        for s in sessions:
            if s.umo == umo:
                return s
    return sessions[0] if sessions else None


# ---------------------------------------------------------------- 结构化数据（给网页视图）


def _stat_payload(stat: Stat, prices: Prices) -> dict:
    """一次请求的明细，前端直接拿去画表。"""
    return {
        "at": _fmt_time(stat.at),
        "at_ts": int(stat.at.timestamp()) if stat.at else None,
        "ctx": stat.ctx,
        "miss": stat.miss,
        "hit": stat.hit,
        "out": stat.out,
        "rate": round(stat.rate * 100, 1),
        "cost": round(stat.cost(prices), 6),
        "cost_miss": round(prices.cost(stat.ctx, 0, stat.out), 6),
        "model": stat.model,
    }


def _session_payload(session: Session, prices: Prices) -> dict:
    """一个会话的概览；没有请求记录时给按字数的粗略估算。"""
    bits = (session.umo or "").split(":")
    kind = _KIND_LABEL.get(bits[1], bits[1]) if len(bits) > 1 else ""
    payload = {
        "cid": session.cid,
        "umo": session.umo,
        "title": session.title,
        "label": session_label(session),
        "kind": kind,
        "updated": _fmt_time(session.updated),
        "updated_ts": int(session.updated.timestamp()) if session.updated else None,
        "messages": session.messages,
        "chars": session.chars,
        "estimated_ctx": session.chars // 2,
        "has_stats": session.last is not None,
        "estimated": session.last is None,
    }
    if session.last is None:
        payload.update(
            {
                "at": None,
                "at_ts": None,
                "ctx": None,
                "miss": None,
                "hit": None,
                "out": None,
                "rate": None,
                "cost": None,
                "cost_miss": None,
                "model": "",
            }
        )
    else:
        payload.update(_stat_payload(session.last, prices))
    return payload


def collect(
    db_path: str | Path,
    cid: str | None = None,
    umo: str | None = None,
    *,
    prices: Prices = Prices(),
    max_ctx: int = 0,
    recent: int = 10,
    limit: int = 200,
) -> dict:
    """一次取全：会话列表 + 选中会话的最近请求明细。

    插件网页视图调这个，返回纯 JSON 可序列化的 dict。
    """
    con = connect(db_path)
    try:
        sessions = fetch_sessions(con)
        selected = pick_session(sessions, cid, umo)
        stats: list[Stat] = []
        if selected is not None:
            stats = fetch_recent(con, selected.cid, limit=max(1, int(recent)))
            if stats:
                selected.last = stats[0]
        items = [_session_payload(s, prices) for s in sessions[: max(1, int(limit))]]
        detail = None
        if selected is not None:
            detail = _session_payload(selected, prices)
            detail["recent"] = [_stat_payload(s, prices) for s in stats]
        return {
            "db": str(db_path),
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "max_ctx": int(max_ctx or 0),
            "prices": {
                "miss": prices.miss * 1_000_000,
                "hit": prices.hit * 1_000_000,
                "out": prices.out * 1_000_000,
            },
            "total": len(sessions),
            "sessions": items,
            "selected": detail,
        }
    finally:
        con.close()


# ---------------------------------------------------------------- 渲染文本


def session_label(session: Session) -> str:
    bits = (session.umo or "").split(":")
    kind = _KIND_LABEL.get(bits[1], bits[1]) if len(bits) > 1 else ""
    sid = bits[2] if len(bits) > 2 else ""
    title = (session.title or "").strip()
    if title and not title.startswith(("群聊", "私聊", "频道")):
        return f"{title}（{kind} {sid}）" if kind else title
    return f"{kind} {sid}".strip() or (session.umo or "未知会话")


def render_current(
    session: Session,
    recent: list[Stat],
    prices: Prices = Prices(),
    max_ctx: int = 0,
    show_model: bool = False,
) -> str:
    lines = [f"【上下文】{session_label(session)}"]

    meta = []
    if session.updated:
        meta.append(f"更新 {_fmt_time(session.updated)}")
    meta.append(f"{_n(session.messages)} 条消息")
    lines.append(" · ".join(meta))

    last = session.last
    if last is None:
        estimate = session.chars // 2
        lines.append("这个会话还没有请求记录")
        if estimate:
            lines.append(f"按消息内容估算，上下文大约 {_n(estimate)} token")
        return "\n".join(lines)

    lines.append(
        f"上次请求：上下文 {_n(last.ctx)} + 输出 {_n(last.out)} = {_n(last.total)} token"
    )
    lines.append(f"缓存命中 {_n(last.hit)}（{_pct(last.hit, last.ctx)}），未命中 {_n(last.miss)}")

    price_line = f"这一次 {_money(last.cost(prices))}"
    price_line += f" · 全未命中约 {_money(prices.cost(last.ctx, 0, last.out))}"
    if max_ctx:
        price_line += f" · 上限 {_n(max_ctx)}，用了 {_pct(last.ctx, max_ctx)}"
    lines.append(price_line)

    if recent and len(recent) > 1:
        lines.append("")
        lines.append(f"最近 {len(recent)} 次")
        for stat in recent:
            at = stat.at.strftime("%H:%M") if stat.at and stat.at.date() == datetime.now().date() else _fmt_time(stat.at)
            line = (
                f"{at}  上下文 {_n(stat.ctx)}  命中 {_pct(stat.hit, stat.ctx)}"
                f"  输出 {_n(stat.out)}  {_money(stat.cost(prices))}"
            )
            if show_model and stat.model:
                line += f"  {stat.model}"
            lines.append(line)

    lines.append("")
    lines.append("发「/上下文全部」看所有会话")
    return "\n".join(lines)


def render_all(
    sessions: list[Session],
    prices: Prices = Prices(),
    limit: int = 15,
    current_cid: str | None = None,
) -> str:
    if not sessions:
        return "【上下文】还没有任何会话记录。"

    lines = [f"【上下文】共 {len(sessions)} 个会话，按最近活动排序"]
    for session in sessions[: max(1, int(limit))]:
        mark = "▶ " if session.cid == current_cid else "  "
        when = _fmt_time(session.updated)
        if session.last:
            size = f"上下文 {_n(session.last.ctx)}"
            hit = f"命中 {_pct(session.last.hit, session.last.ctx)}"
        else:
            size = f"上下文 ≈{_n(session.chars // 2)}"
            hit = "无请求记录"
        lines.append(
            f"{mark}{when}  {_n(session.messages)} 条  {size}  {hit}  {session_label(session)}"
        )
    if len(sessions) > limit:
        lines.append(f"…还有 {len(sessions) - limit} 个更早的会话")
    lines.append("")
    lines.append("发「/上下文」只看当前会话")
    return "\n".join(lines)


def render_help() -> str:
    return (
        "【看上下文】读 AstrBot 的 data_v4.db，看会话有多大、缓存命中多少、这次花了多少钱\n"
        "\n"
        "/上下文          当前会话：消息条数、上下文 token、命中率、花费\n"
        "/上下文今日      今天一共花了多少、几个请求\n"
        "/上下文统计      最近几天的花费、命中率、模型分布（默认 7 天）\n"
        "/上下文全部      所有会话按最近活动排序\n"
        "/上下文帮助      就是这条\n"
        "\n"
        "口径：上下文 = 上一次请求的「未命中 + 命中」token；"
        "花费按插件配置的单价计算（默认 ¥2 / ¥0.04 / ¥8 每百万）。\n"
        "网页视图在面板侧栏「扩展功能 → 看上下文」，有每日趋势、模型/渠道/时段拆分。"
    )


def build_report(
    db_path: str | Path,
    cid: str | None = None,
    umo: str | None = None,
    *,
    mode: str = "current",
    prices: Prices = Prices(),
    max_ctx: int = 0,
    recent: int = 3,
    limit: int = 15,
    show_model: bool = False,
    current_cid: str | None = None,
) -> str:
    """一次性入口：打开库、取数据、渲染。插件与命令行都调它。"""
    con = connect(db_path)
    try:
        sessions = fetch_sessions(con)
        if mode == "all":
            return render_all(
                sessions,
                prices=prices,
                limit=limit,
                current_cid=current_cid or cid,
            )
        session = pick_session(sessions, cid, umo)
        if session is None:
            return "【上下文】还没有任何会话记录。"
        stats = fetch_recent(con, session.cid, limit=max(recent, 1))
        if stats:
            session.last = stats[0]
        return render_current(
            session, stats, prices=prices, max_ctx=max_ctx, show_model=show_model
        )
    finally:
        con.close()
