# 使用手册：从零到能用的每一步

> 每一步都写了「你应该看到什么」。对不上就停在那一步，照着「怎么办」修，不要往下走。

---

## 0. 先回答三个现实问题

### 0.1 机器人跑在哪

**必须有一台 Windows 机器**。原因不是偏好，是物理限制：微信个人号方案要把指令注入 `WeChat.exe` 进程，Linux/Docker 里没有这个进程。

这台机器可以是：你现在这台、一台闲置的旧笔记本、或者 N100 迷你主机（后者更省电，常年开机更合适）。

### 0.2 需要一直开机吗 —— 需要

| 你做的事 | 后果 |
| --- | --- |
| 电脑关机 | 机器人立刻下线 |
| 系统进入睡眠 | 同上（网断了就收不到消息） |
| 微信退出登录 | 下线，且**必须重新扫码**才能恢复 |
| 断网 | 收不到消息；`wcferry` 会重连，但微信掉线不会自动恢复 |

所以它是「一台常开的机器 + 一个保持登录的微信小号」。屏幕可以关（显示器超时），但**系统不能睡眠**，见 [8.1](#81-电源与系统设置)。

没有绕过办法：微信的登录态绑在客户端进程上，云端托管需要非官方协议，风险更高、成本更大。

### 0.3 三种运行方式怎么选

| 方式 | 命令 | 适合 |
| --- | --- | --- |
| 单机一体（推荐） | `run.py --mode embedded` | 就一台 Windows 机器自用，最省事 |
| 分离部署 | Windows 跑 `--mode gateway`，Linux 跑 `--mode core` | 想把模型/记忆放到服务器、Windows 只当网关卡 |
| 只跑核心 | `run.py --mode core` | 只用来验证接口或在 Docker 里跑 |

**如果你只有一台电脑：用 `embedded`，别碰 Docker。** Docker 只在「核心要放到另一台 Linux 机器」时才有意义，而且它救不了"微信必须在 Windows"这件事。

### 0.4 万一微信版本对不上怎么办

不是死路，有三条出路：

1. **换 wcferry 版本去匹配你的微信**（推荐，不动微信）。
2. **换微信版本去匹配 wcferry**（重装微信，历史版本仓库能下）。
3. **微信 4.x 没有 hook**：只能在同机器另装一个 3.9.x，用小号登在 3.9 上（两个客户端可以共存，不同账号）。

版本对应关系（实测，读各版本 `spy.dll` 里的版本串得到）：

| wcferry | 微信 |
| --- | --- |
| 39.4.2.2 / **39.4.5.0** | **3.9.12.17** ← 默认 |
| 39.5.1.0 / 39.5.2.0 | 3.9.12.51 |
| 39.6.0.0 | 3.9.12.56 |

`--mode doctor` 会自动比对这两者，不匹配时直接告诉你该换哪个。

---

## 1. 自检：一条命令看清缺什么

```powershell
cd F:\work\api
.\.venv\Scripts\python.exe run.py --mode doctor
```

正常的样子（还没配 Key 时）：

```
=== 环境自检 ===
[ OK ] 运行平台             Windows
[ OK ] Python 版本        3.12.10
[ OK ] 依赖 wcferry
[WARN] .env 配置          没有 .env
[FAIL] 模型 API Key       为空
[ OK ] 微信客户端          3.9.12.17 @ E:\weichat\WeChat；4.1.11.23 @ E:\weichat\Weixin
[WARN] 微信 4.x           4.1.11.23
[ OK ] 版本匹配            微信 3.9.12.17 = DLL 目标 3.9.12.17
[ OK ] wcferry 端口        10086 可用
```

**只处理 `[FAIL]`，`[WARN]` 可以带着跑。** 每一项后面 `->` 就是动作。

填好 Key 后再跑一次，加上 `--check-api`，它会真发一次最小请求，验证 Key 有效、模型名拼对、余额够：

```powershell
.\.venv\Scripts\python.exe run.py --mode doctor --check-api
```

**你应该看到**：`[ OK ] 模型调用实测  deepseek-chat 回复：正常`

---

## 2. 准备微信客户端

### 2.1 确认版本

设置 → 关于微信 → 版本号，必须是 **3.9.12.17**。

不是的话，两种选择：
- 换 wcferry 版本（见 0.4 表），或
- 装 3.9.12.17 客户端（历史版本仓库 `wechat-windows-versions` 有全部构建）。

### 2.2 关掉自动更新（最容易忽略、后果最严重）

微信 → 设置 → 通用设置 → **取消勾选「有更新时自动升级微信」**。

为什么必须关：微信从 3.9.12.17 自动升到 3.9.12.56，注入 DLL 就废了，而且是**静默失效**——程序还在跑，消息就是收不到。

### 2.3 用小号登录

- 用**不重要的微信号**，不要用主号。
- 不要同时用 4.x 和 3.9 登录同一个号（会被顶下来）。
- 登录后把微信窗口**保持打开**（最小化可以，别关）。

---

### 2.4 已有微信 4.x，怎么再装一个 3.9.12.17（共存）

3.9 和 4.x 是两个独立程序（`WeChat.exe` / `Weixin.exe`），**可以同时安装、同时运行、分别登录不同账号**，互不干扰。

1. 下载 3.9.12.17 安装包：
   <https://github.com/tom-snow/wechat-windows-versions/releases/tag/v3.9.12.17>

   可选校验：SHA256 应为 `4985f96235154fc4176e3972f14709f5f10fc0606e5589075a6da9b6dc7fccd3`，
   数字签名应为 `Tencent Technology (Shenzhen) Company Limited`。
2. 双击安装，**用默认路径 `C:\Program Files\Tencent\WeChat` 就行**。
3. 装完**先别登录**，打开 3.9 的「设置 → 通用设置」，取消勾选自动更新。
4. 用**机器人小号**登录 3.9；4.x 那边继续用你的主号。
5. `run.py --mode doctor` 应出现：

   ```
   [ OK ] 版本匹配            微信 3.9.12.17 = DLL 目标 3.9.12.17
   ```

> **安装器的坑（实测）**：WeChat 3.9 的安装器会**忽略 `/D=` 参数，也会覆盖注册表里的 InstallPath**，强制装到 `C:\Program Files\Tencent\WeChat`。想把目录放别的盘，只能装完后用管理员权限搬走，不建议折腾——它和 4.x 的 `E:\weichat\Weixin` 本来就不冲突。

**如果 3.9.12.17 登录时提示「版本过低」**，说明腾讯已封掉该构建。改用另一组已验证的配对：

- 微信 `3.9.12.51` + `wcferry==39.5.2.0`

改 `requirements-gateway.txt` 里那一行后重装：

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade wcferry==39.5.2.0 -i https://pypi.tuna.tsinghua.edu.cn/simple
.\.venv\Scripts\python.exe run.py --mode doctor
```

---

## 3. 安装依赖

项目里已经有 `.venv`。如果是全新环境：

```powershell
cd F:\work\api
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
.\.venv\Scripts\python.exe -m pip install -r requirements-gateway.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**你应该看到**：`Successfully installed ... wcferry-39.4.5.0 ...`

> 注意 `requirements-gateway.txt` 里钉死了 `wcferry==39.4.5.0`，别用 `pip install wcferry` 裸装——那会装最新版，对应的是 3.9.12.56，跟你的微信对不上。

验证：

```powershell
.\.venv\Scripts\python.exe -c "import wcferry; print(wcferry.__version__)"
```

---

## 4. 配置 .env

### 4.1 最小配置（三行就够跑）

```powershell
Copy-Item .env.example .env
notepad .env
```

```ini
DEEPSEEK_API_KEY=sk-你的key
BOT_NAMES=小助手
TRIGGER_MODE=at
```

就这三行能跑起来，其余全有默认值。

### 4.2 BOT_NAMES 怎么填

填**机器人小号的微信昵称**，必须和微信里显示的一字不差（含空格、表情符号）。

不填也能跑（程序会自动读登录账号昵称），但填上是双保险——`@` 识别有两条通道：微信 xml 里的 `atuserlist`（主）和昵称匹配（兜底）。

多个昵称用英文逗号隔开。

### 4.3 只服务指定群（可选）

```ini
ALLOWED_ROOMS=家庭群,工作群
BLOCKED_ROOMS=广告群
```

填群名称或 roomid 都行。留空 = 被拉进的所有群都服务。黑名单优先级高于白名单。

---

## 5. 校准 @ 识别（**别跳过这一步**）

```powershell
.\.venv\Scripts\python.exe run.py --mode probe
```

**你应该看到**：

```
机器人昵称: 小助手
机器人 wxid: wxid_xxxxxxxxxxxx
现在到群里 @ 一下机器人，观察上面的输出。Ctrl+C 退出。
```

然后去群里 @ 它，终端会打出：

```
----------------------------------------------------------------------
群      : 测试群  id=123456789@chatroom
发送者  : 张三  wxid=wxid_aaa
类型    : text   @我: True   自己: False
内容    : '@小助手 今天几号'
```

**判断标准只有一条：`@我: True`。**

### 三种异常

| 现象 | 原因 | 怎么办 |
| --- | --- | --- |
| 终端什么都没有 | 收不到消息 | 版本没配对（跑 doctor）、微信没登录、或用的是 4.x 客户端 |
| 有消息但 `@我: False` | 昵称没匹配上 | 把 `BOT_NAMES` 填成微信里显示的原名 |
| 只有"自己: True" | 只收到自己发的 | 正常，程序会忽略自己发的消息 |

`probe` 模式**不会调用模型**，可以放心反复试。

---

## 6. 正式启动

```powershell
.\.venv\Scripts\python.exe run.py --mode embedded
```

**你应该看到**：

```
微信网关就绪：小助手 (wxid_xxxxxxxx)
开始监听微信消息…（Ctrl+C 退出）
```

### 验收清单（逐条打勾）

1. 群里 @ 它问一句 → 3 秒内回复。
2. 发 `@小助手 /help` → 返回命令列表。
3. 发 `@小助手 /status` → 返回模型名和消息统计。
4. 发一条**不 @ 它**的消息 → 它保持沉默（这是对的）。
5. 在另一个群 @ 它 → 也能回，且**不知道前一个群聊了什么**（记忆隔离）。
6. 连续问两句 → 第二句能接上第一句的上下文。

---

## 7. 7×24 常开

### 7.1 电源与系统设置

以管理员身份运行 PowerShell：

```powershell
powercfg /change monitor-timeout-ac 10    # 显示器 10 分钟关，省电
powercfg /change standby-timeout-ac 0     # 永不睡眠
powercfg /change hibernate-timeout-ac 0   # 永不休眠
powercfg /change disk-timeout-ac 0        # 硬盘不关
```

### 7.2 开机自启

项目自带 `scripts\start-bot.bat`。注册登录时自启：

```powershell
schtasks /Create /TN "WxAgentBot" /TR "\"F:\work\api\scripts\start-bot.bat\"" /SC ONLOGON /RL HIGHEST /F
```

然后到「任务计划程序」里给这个任务加一条：**如果任务失败，按以下频率重新启动：1 分钟 / 尝试 3 次**。这样进程崩了会自动拉起。

> 用 `ONLOGON` 而不是 `ONSTART`：`ONSTART` 以 SYSTEM 身份运行，看不到你的微信进程。机器需要保持登录状态（可以设自动登录，但注意安全）。

### 7.3 掉线了怎么办

`wcferry` 能处理网络抖动，但**处理不了微信退出登录**——重新登录必须扫码。

| 情况 | 恢复动作 |
| --- | --- |
| 网络短暂断开 | 自动恢复，不用管 |
| 微信提示"在别处登录"/被手机踢下线 | 重新扫码登录微信，再重启网关 |
| 电脑重启 | 微信若设了自动登录会自己起来；否则扫码后重启网关 |

建议每天瞟一眼日志（`logs\bot.out.log`）确认还活着。

### 7.4 Windows 更新

Windows 更新重启会中断服务。设置 → Windows 更新 → 高级选项 → **活动时间**，把常用时段排除掉；或者干脆设成「暂停更新 3 周」，定期手动更新。

---

## 8. Docker 分离部署（可选）

只在「核心要跑到另一台 Linux 机器」时做。**你这台机器目前没装 Docker**，装 Docker Desktop 还需要 WSL2，成本不小——单机自用没必要。

装了之后：

```bash
# Linux 那台
cp .env.example .env && vim .env      # 填 Key 和 GATEWAY_TOKEN
docker compose up -d --build
docker compose logs -f
```

```powershell
# Windows 这台
.\.venv\Scripts\python.exe run.py --mode gateway --core-url http://<Linux的IP>:8000
```

**你应该看到**：`已连接核心 http://...，模型 deepseek-chat`

---

## 9. 日常操作

### 命令（在群里 @ 它）

| 输入 | 作用 |
| --- | --- |
| `@小助手 /help` | 命令列表 |
| `@小助手 /status` | 模型、会话数、触发方式 |
| `@小助手 /reset` | 清空**本群**记忆 |

### 日志

```ini
LOG_LEVEL=INFO
LOG_FILE=logs/bot.log
```

不开 `LOG_FILE` 就只打到控制台。排查问题时把 `LOG_LEVEL=DEBUG`，能看到每条群消息的原文。

### 备份

记忆全在 `data\bot.db`。备份前先停服务（WAL 模式下直接拷可能不一致）：

```powershell
Stop-Process -Name python -ErrorAction SilentlyContinue
Copy-Item data\bot.db data\bot.db.bak
```

### 升级微信之前

先跑 `--mode doctor` 看新版有没有对应的 wcferry。**先确认，再升级**——反过来的代价是机器人静默失效。

---

## 10. 排错对照表

| 症状 | 先查 | 处理 |
| --- | --- | --- |
| `版本匹配 [FAIL]` | 版本表 | 换 wcferry 或换微信 |
| 启动报「微信还没有登录」 | 微信是否已登录 | 先登录再启动 |
| 启动报「开启消息接收失败」 | 版本/权限 | 确认版本；试管理员权限运行 |
| 收不到任何消息 | 是否 4.x 客户端 | 换 3.9.12.17 登录 |
| 收到但 `@我: False` | `BOT_NAMES` | 填微信里显示的原名 |
| 回复 `status` 非 0 | 风控 | 停 30 分钟，别高频 |
| 第一句能回、第二句不回 | 限流 | 冷却 3 秒 / 每分钟 6 条，等一等 |
| 中文乱码 | 控制台编码 | `chcp 65001` 或设 `PYTHONIOENCODING=utf-8` |
| 端口 10086 被占用 | 残留进程 | 杀 `wcf.exe`/`python`，或改 `WCF_PORT` |
| 模型超时 | 网络 | `doctor` 看「模型接口连通」 |

---

## 11. 成本与风险

**电费**：N100 迷你主机满载约 10W，一天 0.24 度电，按 0.6 元/度算约 **0.15 元/天**。旧笔记本约 0.5–1 元/天。

**模型**：DeepSeek 便宜，按群聊问答的量级，一个月通常几块钱。

**风险**（必须知道）：

- 个人号 hook **违反微信协议**，可能被限流、踢下线或封号。
- 用小号、低频、不群发、不加人——这几条能显著降低概率，但不能归零。
- 本项目仅供学习研究，不要用于营销或违法用途。

---

## 一句话总结

```powershell
# 每次开机后要做的，就这三步
.\.venv\Scripts\python.exe run.py --mode doctor      # 确认环境没坏
# 微信保持登录、窗口不关
.\.venv\Scripts\python.exe run.py --mode embedded    # 启动
```