# 微信群聊智能体

在微信群里 **@ 一下**，就能问它问题。它记得这个群之前聊过什么，回答是纯文本、口语化、不长篇大论。

> 第一次用？直接看 **[详细使用手册](docs/USAGE.md)**，那里是逐步骤、带「你应该看到什么」的版本。

架构上刻意拆成两层：

```
┌──────────────────────────┐        HTTP         ┌──────────────────────────┐
│  Windows（贴着微信客户端）│  ───────────────▶   │  Linux / Docker（无状态） │
│  微信网关 wcferry         │   /api/ingest       │  智能体核心 FastAPI       │
│  收消息 / 发消息          │  ◀───────────────   │  DeepSeek + SQLite 记忆   │
└──────────────────────────┘   返回要发的文本     └──────────────────────────┘
```

拆开的原因：微信客户端只能跑在 Windows 上，而"调模型、管记忆、做限流"这些逻辑跟微信无关，应该能跑在任意 Linux 主机 / NAS / 云服务器上。以后要换 QQ 官方机器人或企业微信，**只写一个新网关，核心一行不改**。

---

## ⚠️ 先读这一段：风险与边界

这个项目默认走的是**微信个人号**路线（通过 WeChatFerry 注入微信客户端）：

- **违反微信使用协议**。腾讯可能对账号限流、踢下线或封号，风险由使用者承担。
- **建议只用小号**当机器人，不要用主号。不要把机器人拉进重要的工作群。
- **不要群发、不要营销、不要高频回复**。代码里已经内置了 `COOLDOWN_SECONDS`（每群最小间隔）和 `MAX_REPLIES_PER_MINUTE`（每群每分钟上限），目的就是降低被风控盯上的概率，不建议调成 0。
- 微信客户端升级会**直接失效**。注入 DLL 是按某个微信构建编译的，**wcferry 版本和微信版本必须成对**（本项目锁定 `wcferry==39.4.5.0` + 微信 `3.9.12.17`），并关掉微信自动更新。

版本对应关系是实测出来的（读各版本 wheel 里的 `spy.dll`），不是猜的：

| wcferry | 适配微信 |
| --- | --- |
| 39.4.2.2 / **39.4.5.0** | **3.9.12.17** ← 本项目默认 |
| 39.5.1.0 / 39.5.2.0 | 3.9.12.51 |
| 39.6.0.0 | 3.9.12.56 |

差一个小版本号往往就不兼容。`python run.py --mode doctor` 会直接比对这两者，配不上会告诉你该换哪个。

如果你需要的是**合规、长期、可对外**的方案，请直接把网关换成 QQ 官方机器人开放平台或企业微信智能机器人，核心逻辑可以原样复用——见文末「换成别的平台」。

---

## 5 分钟跑起来

### 0. 前置条件

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11（网关必须在这上面） |
| 微信 | **3.9.12.17**（与 `wcferry==39.4.5.0` 配对），已登录一个**小号**，保持窗口不关 |
| Python | 3.10+（本项目在 3.12 上验证） |
| 模型 | DeepSeek API Key（https://platform.deepseek.com/api_keys ） |

> **只有 4.x 也没关系**：3.9 与 4.x 可以同时安装、同时运行，分别登录不同账号。把 3.9.12.17 装到独立目录（如 `E:\weichat\WeChat39`），用小号登录它，4.x 继续用主号。两个客户端都要关掉自动更新。详见 [USAGE.md 2.4](docs/USAGE.md)。

### 1. 装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-gateway.txt
```

### 2. 写配置

```powershell
Copy-Item .env.example .env
notepad .env
```

最少改这两行：

```ini
DEEPSEEK_API_KEY=sk-你的key
BOT_NAMES=小助手
```

`BOT_NAMES` 填**小号的微信昵称**（可填多个，逗号分隔）。不填也能跑，程序会自动读登录账号的昵称。

### 3. 自检（30 秒，能省掉 90% 的坑）

```powershell
.\.venv\Scripts\python.exe run.py --mode doctor
```

它会检查：Python/依赖、`.env` 与 API Key、数据目录、模型接口连通性、**微信版本与注入 DLL 是否配对**、wcferry 端口占用。

有 `[FAIL]` 就先按 `->` 后面的提示修掉；`[WARN]` 可以继续但要知道原因。

### 4. 先校准 @ 识别（强烈建议）

不同的微信版本，群消息的 `content` 格式略有差异。先跑校准模式，确认 @ 能被认出来：

```powershell
.\.venv\Scripts\python.exe run.py --mode probe
```

然后去群里 @ 一下机器人，看输出：

```
机器人昵称: 小助手
机器人 wxid: wxid_xxxxxxxx
现在到群里 @ 一下机器人，观察上面的输出。Ctrl+C 退出。
----------------------------------------------------------------------
群      : 测试群  id=1234567890@chatroom
发送者  : 张三  wxid=wxid_aaa
类型    : text   @我: True   自己: False
内容    : '@小助手 今天几号'
```

关键是 **`@我: True`**。如果是 `False`，说明自动识别没命中，此时把你的机器人昵称补进 `BOT_NAMES` 再试（走的是昵称兜底匹配）。

### 5. 正式启动

```powershell
.\.venv\Scripts\python.exe run.py --mode embedded
```

看到 `开始监听微信消息…` 后，去群里 @ 它：

```
你：@小助手 明天上海天气怎么样
小助手：我查不到实时天气……（它会老实说不知道，而不是瞎编）
```

`embedded` 是单进程模式：网关和核心在同一个进程里，最适合本机自用。要 7×24 常开的话，把核心丢进 Docker，见下一节。

---

## Docker 部署（核心容器化）

Docker **装不下微信客户端**，所以容器跑的是"智能体核心"，微信网关仍然留在那台开着微信的 Windows 上。这样核心可以跑在更省电、更稳的 Linux 机器上。

### 在 Linux / NAS 上

```bash
cp .env.example .env
vim .env            # 填 DEEPSEEK_API_KEY 和 GATEWAY_TOKEN
docker compose up -d --build
docker compose logs -f
```

`GATEWAY_TOKEN` 建议设一个随机串，核心会校验网关请求带没带这个头。

### 回到 Windows 那台

```powershell
.\.venv\Scripts\python.exe run.py --mode gateway --core-url http://<Linux机器的IP>:8000
```

启动时会打印 `已连接核心 ...，模型 deepseek-chat`，说明两边通了。

**注意**：Windows 那台必须常开，且微信保持登录、窗口别关。如果 Windows 重启，需要手动重新登录微信并重启网关——个人号方案没有真正的无人值守。

---

## 配置项说明

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 空 | 必填，模型密钥 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 换成其他 OpenAI 兼容服务时改这里（如 `https://api.openai.com/v1`） |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 也可用 `deepseek-reasoner`（更慢但推理强） |
| `BOT_NAMES` | 空 | 机器人昵称，多个用逗号隔开；空则自动读取 |
| `TRIGGER_MODE` | `at` | `at` 被 @ 才回 / `prefix` 前缀触发 / `all` 全回（很吵） |
| `TRIGGER_PREFIX` | `/` | prefix 模式下用，填完整命令词如 `/ask` |
| `REPLY_IN_PRIVATE` | `true` | 私聊是否回复 |
| `ALLOWED_ROOMS` | 空 | 白名单，群名或 roomid；空=所有群 |
| `BLOCKED_ROOMS` | 空 | 黑名单，优先于白名单 |
| `ADMIN_WXIDS` | 空 | 允许 `/reset` 的 wxid；空=所有人可清空 |
| `CONTEXT_MESSAGES` | 12 | 每次带多少条最近群消息做上下文 |
| `SUMMARY_TRIGGER` | 40 | 攒够多少条新消息压缩一次摘要 |
| `SUMMARY_KEEP` | 12 | 摘要压缩后保留多少条原文 |
| `COOLDOWN_SECONDS` | 3 | 同一群两次回复的最小间隔 |
| `MAX_REPLIES_PER_MINUTE` | 6 | 每群每分钟回复上限 |
| `MAX_REPLY_CHARS` | 1800 | 单条消息长度上限，超了自动分条 |
| `MODE` | `embedded` | `embedded` / `gateway` / `core` / `probe` / `doctor` |
| `GATEWAY_TOKEN` | 空 | 网关与核心之间的共享密钥 |
| `WCF_PORT` | 10086 | wcferry 的 RPC 端口 |

---

## 它是怎么工作的

一条群消息进来后：

1. **去重**：`msg_id` 已处理过就丢弃（网关重连会重复推送）。
2. **触发判定**：`mode=at` 时看 xml 里的 `<atuserlist>`（比匹配昵称可靠），命中才继续；没 @ 的消息只入库、不回复。
3. **取上下文**：最近 `CONTEXT_MESSAGES` 条原文 + 一段滚动摘要，拼成"群聊记录"给模型。
4. **调模型**：DeepSeek，system 提示词里明确要求"纯文本、别用 Markdown、先给结论、别客套"。
5. **清洗与分条**：去掉 `**`、`#`、`` ` `` 等微信不渲染的符号，超长按段落/句子切分，最多 4 条。
6. **发出去 + 记回自己**：机器人自己的回答也入库，这样追问才有连贯上下文。
7. **后台压缩摘要**：攒够 40 条就在子线程里滚动压缩，不阻塞回复。

记忆用 SQLite 存（`data/bot.db`），按群隔离，一个群聊出花也污染不到另一个群。

### 内置命令

| 命令 | 作用 |
| --- | --- |
| `@机器人 /help` | 看用法 |
| `@机器人 /status` | 看模型、会话数、触发方式 |
| `@机器人 /reset` | 清空本群记忆（`ADMIN_WXIDS` 为空时人人可用） |

---

## 常见问题

**@ 了但没反应？**
先跑 `run.py --mode doctor` 看「版本匹配」这一项：微信版本和 wcferry 的注入 DLL 对不上时，消息能收但 `@我` 永远 `False`，或者干脆收不到。其次看 `--mode probe` 里 `@我` 是不是 `True`、`ALLOWED_ROOMS` 有没有把这个群排除掉、日志里有没有 `触发限流`。

**报 `初始化失败，wcf.exe 退出码`？**
微信版本不匹配，或者 wcferry 没权限注入。确认版本、把微信完全退出后重启，必要时用管理员权限运行终端。

**发消息 `status` 非 0？**
通常是被风控临时限制发送。停一会儿，别立刻重试——代码里已经刻意不做发送重试。

**能不能同时服务多个群？**
可以，默认就支持所有被拉进去的群，每个群记忆独立。想只服务固定几个群，用 `ALLOWED_ROOMS`。

**能不能让它主动说话 / 定时播报？**
当前版本只在被 @ 时回复。要做定时播报，可以在 `run.py` 里加个 `threading.Timer` 调 `gateway.send_text(roomid, text)`。

**模型答得太长/太官方？**
改 `.env` 里的 `EXTRA_SYSTEM_PROMPT` 追加要求，例如：`回答不超过两句话，像群友聊天一样随意`。

---

## 换成别的平台

核心（`src/wxagent/agent.py`）不知道微信的存在，只认 `IncomingMessage` 和 `Reply`。所以要换平台，**只需实现一个新的 `BaseGateway` 子类**：

```python
# src/wxagent/gateway/qq_official_gw.py（示意）
from .base import BaseGateway

class QQOfficialGateway(BaseGateway):
    def run_forever(self):
        while True:
            event = self._poll()                       # 收 QQ 官方事件
            msg = IncomingMessage(chat_id=event.group_id, content=event.content, ...)
            for reply in self.dispatch(msg):           # 复用同一套智能体
                self.send_text(event.group_id, reply.text)
```

对照表：

| 平台 | 接入方式 | 合规性 | 门槛 |
| --- | --- | --- | --- |
| 微信个人号 | WeChatFerry 注入（本项目） | ⚠️ 违反协议 | 低，能进任意群 |
| 企业微信 | 自建应用 / 智能机器人回调 | ✅ 官方支持 | 需企业主体 |
| QQ 群 | 官方开放平台 Bot（`botpy`） | ✅ 官方支持 | 需审核，群聊能力要申请 |
| 微信公众号 | 服务号被动回复 | ✅ 官方支持 | 只能一对一，进不了群 |

---

## 项目结构

```
api/
├── run.py                       # 统一入口：embedded / gateway / core / probe
├── .env.example                 # 全部配置项说明
├── Dockerfile                   # 核心镜像（不含微信客户端）
├── docker-compose.yml           # 一键起核心
├── requirements.txt             # 核心依赖（跨平台）
├── requirements-gateway.txt     # 网关依赖（Windows + wcferry）
├── src/wxagent/
│   ├── config.py                # 环境变量加载 + 日志
│   ├── store.py                 # SQLite：消息、摘要、去重
│   ├── memory.py                # 滑动窗口 + 滚动摘要
│   ├── prompt.py                # 系统提示词（群聊人格设定）
│   ├── llm.py                   # DeepSeek / OpenAI 兼容客户端
│   ├── pipeline.py              # 触发判定、Markdown 清洗、分条、限流
│   ├── agent.py                 # 核心编排（平台无关）
│   ├── doctor.py                # 环境自检（版本配对、端口、Key）
│   ├── docs/USAGE.md            # 详细使用手册
│   ├── core_api.py              # 把核心暴露成 HTTP
│   └── gateway/
│       ├── base.py              # 网关抽象
│       ├── remote.py            # 调用远端核心
│       └── wcferry_gw.py        # 微信个人号实现
└── tests/                       # 29 个单测，不需要微信和网络
```

---

## 验证状态

**已验证**（本机实际跑过）：

- `pytest` 29 个用例全绿：@ 识别、窄空格、Markdown 清洗、长文分条、限流、去重、群隔离、记忆压缩、LLM 异常降级。
- 核心 HTTP 接口：`/healthz`、`/api/ingest`（含 token 校验 401）、`/api/bot-names` 全部通。
- `run.py --mode core` 真实起 uvicorn 服务，`/healthz` 返回 200。
- wcferry 39.6.0 的 API 是照着**实际源码**核对的（`is_at` 读 xml 的 `<atuserlist>`、`get_info_by_wxid` 未实现所以改用 `get_alias_in_chatroom` + `query_sql`）。

**未验证**（需要你登录微信才能测）：

- 真实的微信收消息 / 发消息闭环。所以第 4 步的 `--mode probe` 别跳过。
- 微信/wcferry 之外的版本组合：上表是我逐个下载 wheel、读 `spy.dll` 里的版本串得出的，只有 39.4.5.0 + 3.9.12.17 这一组做了 API 层面对齐核对。

---

## 免责声明

本项目仅用于个人学习与技术研究。使用微信个人号 hook 违反《微信软件许可及服务协议》，可能导致账号被限制或封禁，风险由使用者自行承担。请勿用于商业营销、群发骚扰或任何违法用途。