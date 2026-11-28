# 看上下文 · astrbot_plugin_ctx_view

在 AstrBot 里直接看**这个会话现在有多大、缓存命中多少、上一次请求花了多少钱**，
也能看**今天 / 本周 / 本月一共花了多少、花在哪个模型和渠道上**。

三种入口，同一套数据：

- **聊天命令**：`/上下文`、`/上下文今日`、`/上下文统计`、`/上下文全部`、`/上下文帮助`，纯文本输出，QQ 里也不会走样。
- **面板界面**：侧栏「扩展功能 → 看上下文」，顶部是费用概览，下面分「会话 / 统计」两个标签。
- **本地脚本**：`ctx_core.py` 与 `ctx_stats.py` 不依赖 AstrBot，可以直接 import。

适配 AstrBot **4.30+** 的事件溯源存储（`conversation_events` + `provider_stats`），
不需要额外依赖，纯标准库读 `data_v4.db`。

---

## 界面

打开 AstrBot 面板，侧栏 **扩展功能 → 看上下文**（路由 `/plugin-view/astrbot_plugin_ctx_view/context`）。

### 顶部概览

| 区域 | 内容 |
| --- | --- |
| 五块概览 | **今日 / 本周 / 本月 / 累计**花费，**缓存命中**率（附累计省下多少钱） |
| 预算条 | 配了日预算/月预算才出现，两条进度条，超过 80% 变黄、100% 变红 |
| 峰谷徽标 | 开了峰谷计价才出现，显示当前是峰时还是谷时、还有多久切换 |

### 会话标签

| 区域 | 内容 |
| --- | --- |
| 左栏 | 所有会话（按最近活动排序），显示消息数、上下文 token、本次命中率、**会话累计花费**，可搜索 |
| 三块指标 | **上下文**（上次请求的输入 token，及占上限百分比）、**缓存命中**（命中率 + 命中/未命中明细）、**这一次花费**（实际花费 + 全未命中对照价） |
| 会话累计 | 这个会话所有请求的花费之和、请求次数、按 token 加权的累计命中率 |
| 命中比例条 | 蓝色 = 命中，灰色 = 未命中，一眼看出前缀有没有被缓存 |
| 最近请求 | 最近 10 次请求的时间、上下文、命中率、输出、花费、模型 |

### 统计标签

| 区域 | 内容 |
| --- | --- |
| 费用趋势 | 每日花费柱状图，可切近 7 / 14 / 30 天，悬停看当天请求数与命中率 |
| 计费口径 | 右上角下拉切「官方口径 · 峰谷计价」/「原价口径 · 上限估算」，切完立刻重算 |
| 模型 | 请求数、花费、占比、命中率 |
| 渠道 | 私聊 / 群聊 / 频道的请求数、花费、占比 |
| 时段分布 | 24 小时花费分布（按东八区） |
| 最近失败 | 最近 12 条非 completed 请求（error / aborted），附平均 / P50 / P95 延迟 |
| 导出 CSV | 把趋势和模型拆分导出成表格 |

自动刷新勾上后每 15 秒重读一次，页面切到后台时暂停。
页面通过 AstrBot 官方插件视图桥（`window.AstrBotPluginView`）调插件自己的 Web API，
只读数据库，不写入、不改配置。主题色跟随面板的亮色 / 暗色。

## 命令

| 命令 | 别名 | 作用 |
| --- | --- | --- |
| `/上下文` | `看上下文`、`ctx`、`上下文长度` | 当前会话明细 + 今日/本周/本月/累计一小段 |
| `/上下文今日` | `ctx今日` | 今天一共花了多少、几个请求 |
| `/上下文统计` | `上下文7天`、`ctxstats` | 最近 `stats_days` 天（默认 14）的汇总、模型分布、延迟 |
| `/上下文全部` | `全部上下文`、`所有上下文`、`ctxall` | 所有会话按最近活动排序 |
| `/上下文帮助` | `ctxhelp` | 用法说明 |

输出示例：

```text
【上下文】私聊 3850193645
更新 11-23 12:48 · 294 条消息
上次请求：上下文 62,691 + 输出 175 = 62,866 token
缓存命中 60,416（96.4%），未命中 2,275
这一次 ¥0.0084 · 全未命中约 ¥0.127 · 上限 1,000,000，用了 6.3%

最近 3 次
12:48  上下文 62,691  命中 96.4%  输出 175  ¥0.0084

今日 ¥0.576（28 次） · 本周 ¥15.01 · 本月 ¥67.95 · 累计 ¥108.26
```

## 口径

- **上下文 token** = 该会话最新一条 `provider_stats` 的 `token_input_other + token_input_cached`。
  这是模型上一次真正看到的量，比按字数估算准。
- **消息条数** = 最近一次 `context.rebased` 快照的 `messages` 长度 + 之后新增的 `message.appended`。
  没有任何请求记录的会话（老群聊）按 payload 字符数估算：`字符数 // 2`，界面上标 `≈`。
- **单次花费** = 未命中 × `price_miss` + 命中 × `price_hit` + 输出 × `price_out`，
  默认按 2026-11-02 的价格 ¥2 / ¥0.04 / ¥8 每百万 token；再乘该请求**发生时刻**的峰谷系数
  （见下面的「峰谷计价」），所以谷时的单条请求也是按五折算的。
- **缓存省下** = 把命中的 token 按未命中价重算后的差额，只用于展示，不影响扣费。
- **会话累计 / 今日 / 本月**：逐条累加每次请求的花费，**不是**把最后一次的上下文反复相加。
- **统计窗口**：按请求发生时刻的**东八区本地日**分桶；库里存的是 UTC，显示时 +8。
- **峰谷计价 / 计费口径**：默认走**官方 API 口径**，按每条请求发生的时刻判断峰谷：
  北京时间周一至周五（不含法定节假日）的 `09:00–12:00`、`14:00–18:00` 是**峰时**，按原价；
  其余所有时间（两个峰时之间的午休、夜间、周末、节假日全天）是**谷时**，乘 `offpeak_discount`
  （默认 0.5，即五折）。峰时窗口、工作日、节假日/调休上班日都能在配置里改，见下表。
  网页统计页可随时切「原价口径」：不套折扣，当**上限估算**看，两个口径的差额就是谷时省下的。
- 花费是**按 token 单价估算**，不包含平台赠送、余额抵扣或失败请求的特殊计费；
  官方口径下与官方账单的差异只剩「provider_stats 少记的那部分请求」。

## 安装

### 从插件市场

在 AstrBot 面板「插件市场」里搜索「看上下文」或 `ctx_view`，一键安装。

### 上传 zip

面板「插件 → 安装插件 → 上传文件」，选择打包好的 zip（根目录直接是 `metadata.yaml` / `main.py`，
或只有一个顶层目录，两种都能识别）。

### 手动拷贝

把整个 `astrbot_plugin_ctx_view` 目录放进：

```text
<AstrBot 数据目录>/plugins/astrbot_plugin_ctx_view
```

然后重载插件或重启 AstrBot。4.30 默认不开文件热重载，改动后需要重载一次。

## 配置

面板「插件 → 看上下文 → 配置」：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `db_path` | 空 | `data_v4.db` 路径，留空自动探测（数据目录、launcher 实例目录） |
| `max_context_tokens` | 1000000 | 只用来算「用了百分之几」，填 0 不显示 |
| `price_miss` / `price_hit` / `price_out` | 2.0 / 0.04 / 8.0 | 元 / 百万 token |
| `model_prices` | 空 | 按模型覆盖单价，JSON，例如 `{"deepseek-reasoner": {"miss": 4, "hit": 0.04, "out": 16}}`；没写到的模型用默认单价 |
| `daily_budget` / `monthly_budget` | 0 | 预算（元），0 表示不启用 |
| `billing_mode` | official | 默认计费口径：official = 官方 API 峰谷价 / list = 原价口径（上限估算） |
| `offpeak_enabled` | true | 是否套峰谷价（关掉就等于原价口径） |
| `peak_windows` | 09:00–12:00, 14:00–18:00 | 峰时窗口（东八区，含开始不含结束），JSON 二维数组或 `09:00-12:00,14:00-18:00` |
| `peak_weekdays` | 1-5 | 按工作日计价的星期，1=周一 … 7=周日 |
| `peak_holidays` | 空 | 法定节假日（全天谷时），逗号分隔 `YYYY-MM-DD` |
| `peak_workdays` | 空 | 调休上班日（按工作日计价），逗号分隔 `YYYY-MM-DD` |
| `offpeak_discount` | 0.5 | 谷时折扣系数，0.5 = 五折 |
| `stats_days` | 14 | 网页统计与 `/上下文统计` 默认看几天（1–90） |
| `recent_count` | 3 | 聊天命令里显示最近几次请求 |
| `all_limit` | 15 | `/上下文全部` 最多列几个 |
| `show_model` | false | 聊天输出里带模型名 |

## 文件结构

```text
astrbot_plugin_ctx_view/
├── metadata.yaml          # 插件元信息 + views 声明
├── main.py                # 插件壳：命令、配置、Web API 注册
├── ctx_core.py            # 单个会话的上下文明细（纯标准库）
├── ctx_stats.py           # 全表费用聚合：日 / 模型 / 渠道 / 小时 / 预算（纯标准库）
├── _conf_schema.json      # 配置项
├── README.md
├── LICENSE
└── views/
    └── context/
        ├── index.html     # 面板页面
        ├── style.css
        └── app.js
```

两个核心模块都不 import astrbot，只依赖标准库，可以直接 import 复用：

```python
import ctx_core, ctx_stats

prices = ctx_core.Prices(2.0, 0.04, 8.0)
data = ctx_core.collect(ctx_core.find_db(), prices=prices)
print(data["selected"]["label"], data["selected"]["ctx"])

summary = ctx_stats.compute(ctx_core.find_db(), days=14, prices=prices)
print(summary["today"]["cost"], summary["by_model"])
```

## 已知限制

- 只读 `data_v4.db`，数据库读不到时页面会提示 404，命令会提示找不到数据库。
- 会话列表来自 `conversations_v3` 表；4.30 之前的老库结构不同，不支持。
- 统计只覆盖 `provider_stats` 里有记录的请求；被中断且没写入 token 的请求只计入失败次数。
- 花费是估算值，以平台账单为准；DeepSeek 的峰谷价格若调整，请自行核对 `peak_windows`、`peak_weekdays` 与单价。

## 发布到插件市场

AstrBot 的插件市场用 GitHub 仓库托管插件，本插件已托管在
<https://github.com/kkbeee/astrbot_plugin_ctx_view>（`metadata.yaml` 里的 `repo` 已填好）。

1. 打开 <https://cloud.astrbot.app/publish>，用 AstrBot Cloud 账号登录，填仓库地址提交。
   市场会自己拉仓库打包，zip 上限 16 MB（这个插件约 100 KB）。
2. 本地现成的包在 `dist/astrbot_plugin_ctx_view-1.3.1.zip`，面板「安装插件 → 上传」可以直接装。
3. 更新版本时改 `metadata.yaml` 的 `version`，push 之后市场刷新即可。

## 开发

插件视图的约定见 AstrBot 官方文档 `docs/zh/dev/star/guides/plugin-views/`：
视图放在 `views/<name>/index.html`，在 `metadata.yaml` 里用 `views:` 声明 `name` / `target` / `title` / `description`。
前端通过桥调用后端：

```js
const bridge = window.AstrBotPluginView;
await bridge.ready();
const data = await bridge.apiGet("overview", { cid, recent: 10 });
const stats = await bridge.apiGet("stats", { days: 14 });
```

后端：

```python
context.register_web_api(
    "/astrbot_plugin_ctx_view/overview", self.page_overview, ["GET"], "上下文概览"
)
context.register_web_api(
    "/astrbot_plugin_ctx_view/stats", self.page_stats, ["GET"], "费用统计"
)
```

## 许可

MIT
