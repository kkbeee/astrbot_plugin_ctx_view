# 看上下文 · astrbot_plugin_ctx_view

在 AstrBot 里直接看**这个会话现在有多大、缓存命中多少、上一次请求花了多少钱**。

两种入口，同一套数据：

- **聊天命令**：`/上下文`、`/上下文全部`、`/上下文帮助`，纯文本输出，QQ 里也不会走样。
- **面板界面**：侧栏「扩展功能 → 看上下文」，左侧翻会话，右侧看统计、命中比例和最近请求。

适配 AstrBot **4.30+** 的事件溯源存储（`conversation_events` + `provider_stats`），
不需要额外依赖，纯标准库读 `data_v4.db`。

---

## 界面

打开 AstrBot 面板，侧栏 **扩展功能 → 看上下文**（路由 `/plugin-view/astrbot_plugin_ctx_view/context`）。

| 区域 | 内容 |
| --- | --- |
| 左栏 | 所有会话（按最近活动排序），显示消息数、上下文 token、命中率，可搜索 |
| 顶部三块 | **上下文**（上次请求的输入 token，及占上限百分比）、**缓存命中**（命中率 + 命中/未命中明细）、**这一次花费**（实际花费 + 全未命中对照价） |
| 命中比例条 | 蓝色 = 命中，灰色 = 未命中，一眼看出前缀有没有被缓存 |
| 最近请求 | 最近 10 次请求的时间、上下文、命中率、输出、花费、模型 |
| 自动刷新 | 勾上后每 15 秒重读一次，页面切到后台时暂停 |

页面通过 AstrBot 官方插件视图桥（`window.AstrBotPluginView`）调用插件自己的 Web API，
只读数据库，不写入、不改配置。主题色跟随面板的亮色 / 暗色。

## 命令

| 命令 | 别名 | 作用 |
| --- | --- | --- |
| `/上下文` | `看上下文`、`ctx`、`上下文长度` | 当前会话：消息条数、上下文 token、命中率、花费 |
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
```

## 口径

- **上下文 token** = 该会话最新一条 `provider_stats` 的 `token_input_other + token_input_cached`。
  这是模型上一次真正看到的量，比按字数估算准。
- **消息条数** = 最近一次 `context.rebased` 快照的 `messages` 长度 + 之后新增的 `message.appended`。
  没有任何请求记录的会话（老群聊）按 payload 字符数估算：`字符数 // 2`，界面上标 `≈`。
- **花费** = 未命中 × `price_miss` + 命中 × `price_hit` + 输出 × `price_out`，默认按 2026-11-02 的价格
  ¥2 / ¥0.04 / ¥8 每百万 token。
- 库里存 UTC，显示时按东八区 +8。

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
| `recent_count` | 3 | 聊天命令里显示最近几次请求 |
| `all_limit` | 15 | `/上下文全部` 最多列几个 |
| `show_model` | false | 聊天输出里带模型名 |

## 文件结构

```text
astrbot_plugin_ctx_view/
├── metadata.yaml          # 插件元信息 + views 声明
├── main.py                # 插件壳：命令、配置、Web API 注册
├── ctx_core.py            # 统计核心（纯标准库，命令行也能复用）
├── _conf_schema.json      # 配置项
├── README.md
├── LICENSE
└── views/
    └── context/
        ├── index.html     # 面板页面
        ├── style.css
        └── app.js
```

`ctx_core.py` 不 import astrbot，只依赖标准库，可以直接 import 复用：

```python
import ctx_core

prices = ctx_core.Prices(2.0, 0.04, 8.0)
data = ctx_core.collect(ctx_core.find_db(), prices=prices)
print(data["selected"]["label"], data["selected"]["ctx"])
```

## 已知限制

- 只读 `data_v4.db`，数据库读不到时页面会提示 404，命令会提示找不到数据库。
- 会话列表来自 `conversations_v3` 表；4.30 之前的老库结构不同，不支持。
- 花费是**按 token 单价估算**，不包含平台赠送、折扣或失败请求的特殊计费。

## 发布到插件市场

AstrBot 的插件市场用 GitHub 仓库托管插件：

1. 把整个目录推到 GitHub（仓库名建议就叫 `astrbot_plugin_ctx_view`），并在 `metadata.yaml` 里补上
   `repo: https://github.com/<你>/astrbot_plugin_ctx_view`。
2. 打开 <https://cloud.astrbot.app/publish>，用 AstrBot Cloud 账号登录，填仓库地址提交。
   市场会自己拉仓库打包，zip 上限 16 MB（这个插件约 20 KB）。
3. 本地现成的包在 `dist/astrbot_plugin_ctx_view-1.1.0.zip`，面板「安装插件 → 上传」可以直接装。

## 开发

插件视图的约定见 AstrBot 官方文档 `docs/zh/dev/star/guides/plugin-views/`：
视图放在 `views/<name>/index.html`，在 `metadata.yaml` 里用 `views:` 声明 `name` / `target` / `title` / `description`。
前端通过桥调用后端：

```js
const bridge = window.AstrBotPluginView;
await bridge.ready();
const data = await bridge.apiGet("overview", { cid, recent: 10 });
```

后端：

```python
context.register_web_api(
    "/astrbot_plugin_ctx_view/overview", self.page_overview, ["GET"], "上下文概览"
)
```

## 许可

MIT
