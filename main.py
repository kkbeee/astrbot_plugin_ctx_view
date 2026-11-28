"""看上下文：在聊天或面板里看会话的上下文大小、缓存命中与花费。

原来靠 看上下文.bat 调 tools/ctx_now.py 读 data_v4.db 的 conversations 表；
AstrBot 4.30 把会话改成事件溯源存储（conversations_v3 + conversation_events），
旧脚本就报 no such table。这个插件按新结构读，提供聊天命令，
并在面板侧栏「扩展功能 → 看上下文」里提供一个图形页面。

1.2.0 起在 ctx_core 的「当前会话」之外加了 ctx_stats 统计层：
今日/本周/本月/累计、按天趋势、模型与渠道拆分、预算、峰谷折扣。
1.3.0 起默认为「官方 API 口径」：按官方峰谷价算，跟官方账单对得上；
1.3.1 把峰谷边界改成官方规则（工作日 09:00–12:00、14:00–18:00 原价，其余五折）；
1.3.2 修单次请求的花费：表格每一行和「这一次」也按请求发生的时刻打折，
        之前只有汇总套了谷时折扣，逐条明细还是原价。
网页统计页可在官方口径 / 原价口径之间切换。
"""

from __future__ import annotations

import asyncio
import json

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request

try:  # 同目录的核心逻辑（插件以包的方式加载）
    from . import ctx_core, ctx_stats
except ImportError:  # pragma: no cover - 兜底，兼容个别加载方式
    import ctx_core  # type: ignore[no-redef]
    import ctx_stats  # type: ignore[no-redef]

PLUGIN_NAME = "astrbot_plugin_ctx_view"
PLUGIN_VERSION = "1.3.2"


@register(
    PLUGIN_NAME,
    "dsh",
    "看会话的上下文大小、缓存命中率与花费（聊天命令 + 面板侧栏页面，适配 AstrBot 4.30 事件溯源存储）",
    PLUGIN_VERSION,
)
class CtxViewPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config: dict = dict(config or {})
        self._db_path = self._cfg_str("db_path", "") or ctx_core.find_db()
        if self._db_path:
            logger.info(f"[ctx_view] 已加载，数据库：{self._db_path}")
        else:
            logger.warning("[ctx_view] 没找到 data_v4.db，请在插件配置里手填 db_path")
        try:
            context.register_web_api(
                f"/{PLUGIN_NAME}/overview",
                self.page_overview,
                ["GET"],
                "读取会话上下文用量（看上下文网页视图）",
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/stats",
                self.page_stats,
                ["GET"],
                "读取费用统计（按天/模型/渠道/小时，看上下文网页视图）",
            )
        except Exception:  # noqa: BLE001 - 老版本没有 Web API 时不影响聊天命令
            logger.exception("[ctx_view] 注册网页视图接口失败，网页视图将不可用")

    # ------------------------------------------------------------------ 配置
    def _cfg(self, key: str, default):
        value = self.config.get(key, default)
        if value is None or value == "":
            return default
        return value

    def _cfg_str(self, key: str, default: str = "") -> str:
        return str(self._cfg(key, default)).strip()

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _prices(self) -> ctx_core.Prices:
        return ctx_core.Prices(
            miss=self._cfg_float("price_miss", 2.0) / 1_000_000,
            hit=self._cfg_float("price_hit", 0.04) / 1_000_000,
            out=self._cfg_float("price_out", 8.0) / 1_000_000,
        )

    def _book(self) -> ctx_stats.PriceBook:
        raw = self._cfg_str("model_prices", "")
        book = ctx_stats.parse_price_book(self._prices(), raw)
        if raw and not book.overrides:
            logger.warning("[ctx_view] model_prices 解析失败，按默认单价计算")
        return book

    def _offpeak(self) -> ctx_stats.OffPeak:
        return ctx_stats.build_offpeak(
            enabled=bool(self._cfg("offpeak_enabled", True)),
            windows=self._cfg_str("peak_windows", ""),
            weekdays=self._cfg_str("peak_weekdays", ""),
            holidays=self._cfg_str("peak_holidays", ""),
            workdays=self._cfg_str("peak_workdays", ""),
            discount=self._cfg_float("offpeak_discount", 0.5),
        )

    def _basis(self) -> str:
        """默认计费口径：official = 官方 API 峰谷价；list = 原价上限。"""
        return ctx_stats.normalize_basis(self._cfg_str("billing_mode", "official"))

    def _request_basis(self) -> str:
        """网页可临时切口径（?basis=official|list），没带就按配置里的默认。"""
        try:
            raw = request.query.get("basis") or ""
        except Exception:  # noqa: BLE001 - 不在 web 请求里时退回配置
            raw = ""
        return ctx_stats.normalize_basis(raw or self._basis())

    def _factor(self, basis: str | None = None) -> ctx_core.Factor | None:
        """按计费口径给单次请求一个折扣系数；原价口径（list）不打折。"""
        if ctx_stats.normalize_basis(basis or self._basis()) == "list":
            return None
        return self._offpeak().factor

    def _stats_days(self, value: object = None) -> int:
        try:
            days = int(value if value is not None else self._cfg("stats_days", 14))
        except (TypeError, ValueError):
            days = 14
        return max(1, min(days, 90))

    def _budgets(self) -> tuple[float, float]:
        return (
            self._cfg_float("daily_budget", 0.0),
            self._cfg_float("monthly_budget", 0.0),
        )

    def _options(self, mode: str) -> dict:
        return {
            "mode": mode,
            "prices": self._prices(),
            "factor": self._factor(),
            "max_ctx": self._cfg_int("max_context_tokens", 1_000_000),
            "recent": self._cfg_int("recent_count", 3),
            "limit": self._cfg_int("all_limit", 15),
            "show_model": bool(self._cfg("show_model", False)),
        }

    # ------------------------------------------------------------------ 取数
    async def _current_cid(self, event: AstrMessageEvent, umo: str) -> str | None:
        try:
            manager = self.context.conversation_manager
            return await manager.get_curr_conversation_id(umo)
        except Exception as exc:  # noqa: BLE001 - 接口变更时退回 umo 匹配
            logger.debug(f"[ctx_view] 读取当前会话 id 失败：{exc}")
            return None

    async def _summary(self, days: int | None = None, basis: str | None = None) -> dict:
        daily, monthly = self._budgets()
        return await asyncio.to_thread(
            ctx_stats.compute,
            self._db_path,
            days=days or self._stats_days(),
            prices=self._prices(),
            book=self._book(),
            off=self._offpeak(),
            basis=basis or self._basis(),
            daily_budget=daily,
            monthly_budget=monthly,
        )

    async def _report(self, event: AstrMessageEvent, mode: str, days: int | None = None) -> str:
        if not self._db_path:
            return "找不到 data_v4.db。请在 AstrBot 面板的插件配置里填写 db_path。"
        umo = getattr(event, "unified_msg_origin", "") or ""
        cid = await self._current_cid(event, umo)
        try:
            if mode == "stats":
                span = self._stats_days(days)
                data = await self._summary(span)
                return ctx_stats.render_stats(data, span)
            report = await asyncio.to_thread(
                ctx_core.build_report,
                self._db_path,
                cid,
                umo,
                current_cid=cid,
                **self._options(mode),
            )
            if mode == "current":
                summary = await self._summary(1)
                report = f"{report}\n\n{ctx_stats.render_brief(summary)}"
            return report
        except FileNotFoundError:
            return f"数据库不见了：{self._db_path}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("[ctx_view] 生成上下文报告失败")
            return f"读上下文失败：{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------ 网页视图
    async def page_overview(self):
        """网页视图数据：全局概览 + 会话列表 + 选中会话的最近请求明细。"""
        if not self._db_path:
            return error_response(
                "没找到 data_v4.db，请在插件配置里填写 db_path。", status_code=503
            )
        cid = (request.query.get("cid") or "").strip() or None
        try:
            recent = int(request.query.get("recent", 10))
        except (TypeError, ValueError):
            recent = 10
        basis = self._request_basis()
        try:
            data = await asyncio.to_thread(
                ctx_core.collect,
                self._db_path,
                cid,
                None,
                prices=self._prices(),
                factor=self._factor(basis),
                max_ctx=self._cfg_int("max_context_tokens", 1_000_000),
                recent=max(1, min(recent, 50)),
                limit=500,
            )
            summary = await self._summary(basis=basis)
        except FileNotFoundError:
            return error_response(f"数据库不见了：{self._db_path}", status_code=404)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[ctx_view] 读取网页视图数据失败")
            return error_response(
                f"读取失败：{type(exc).__name__}: {exc}", status_code=500
            )

        totals = summary.get("sessions") or {}
        for item in data.get("sessions") or []:
            item.update(totals.get(item.get("cid"), {}))
        if data.get("selected"):
            data["selected"].update(totals.get(data["selected"].get("cid"), {}))
        data["summary"] = {
            key: summary[key]
            for key in ("now", "days", "basis", "basis_label", "peak", "budget", "all", "today", "week", "month", "latency")
            if key in summary
        }
        return json_response(data)

    async def page_stats(self):
        """统计视图数据：按天趋势、模型/渠道/小时拆分、失败请求。"""
        if not self._db_path:
            return error_response(
                "没找到 data_v4.db，请在插件配置里填写 db_path。", status_code=503
            )
        days = self._stats_days(request.query.get("days"))
        try:
            data = await self._summary(days, basis=self._request_basis())
        except FileNotFoundError:
            return error_response(f"数据库不见了：{self._db_path}", status_code=404)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[ctx_view] 读取统计视图数据失败")
            return error_response(
                f"读取失败：{type(exc).__name__}: {exc}", status_code=500
            )
        return json_response(data)

    # ------------------------------------------------------------------ 命令
    @filter.command("上下文", alias={"看上下文", "ctx", "上下文长度"}, desc="看看当前会话的上下文有多大、花了多少钱")
    async def cmd_ctx(self, event: AstrMessageEvent):
        """当前会话：消息条数、上下文 token、缓存命中率、这一次的花费。"""
        yield event.plain_result(await self._report(event, "current"))

    @filter.command("上下文今日", alias={"ctx今日"}, desc="今天一共花了多少钱、几个请求")
    async def cmd_ctx_today(self, event: AstrMessageEvent):
        """今天的汇总 + 当前会话的这一次。"""
        yield event.plain_result(await self._report(event, "stats", 1))

    @filter.command("上下文统计", alias={"上下文花费", "ctxstats"}, desc="最近几天的花费、命中率与模型分布")
    async def cmd_ctx_stats(self, event: AstrMessageEvent):
        """最近 stats_days 天（默认 14）的汇总。"""
        yield event.plain_result(await self._report(event, "stats"))

    @filter.command("上下文全部", alias={"全部上下文", "所有上下文", "ctxall"}, desc="列出所有会话的上下文大小与花费")
    async def cmd_ctx_all(self, event: AstrMessageEvent):
        """所有会话按最近活动排序。"""
        yield event.plain_result(await self._report(event, "all"))

    @filter.command("上下文帮助", alias={"ctxhelp"}, desc="看上下文的用法说明")
    async def cmd_ctx_help(self, event: AstrMessageEvent):
        """用法说明。"""
        yield event.plain_result(ctx_core.render_help())

    async def terminate(self):
        """插件卸载/重载时不需要清理什么东西。"""
