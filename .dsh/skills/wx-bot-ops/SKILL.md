---
name: wx-bot-ops
description: 运维 F:\work\api 这个微信群智能体（微信 4.x UI Automation 网关）时使用。包含启停命令、开机自启、日志位置、可调参数、以及微信 4.x 无障碍/会话解析/发送等已踩过的坑。
whenToUse: 用户说「机器人不回/回得慢/重启一下/改配置/怎么开机自启/为什么没联网」，或需要在这台机器上维护这个网关时。
---

# 微信群智能体 运维手册

项目在 `F:\work\api`，Python venv 在 `.venv`。机器人账号昵称「城南」（小号），
驱动方式是 Windows UI Automation，**没有注入，也没有 hook**。

## 一、启停

```powershell
cd F:\work\api

# 启动（后台，单实例锁保证重复启动无害）
Start-Process -FilePath ".\.venv\Scripts\python.exe" -ArgumentList 'run.py','--mode','uia' `
  -RedirectStandardOutput 'logs\stdout.log' -RedirectStandardError 'logs\stderr.log' -WindowStyle Hidden

# 停止：必须按命令行匹配，不能只按进程名
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*run.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

**为什么不能按进程名杀**：venv 的 `python.exe` 是启动器，会再拉起真正的解释器，
父子命令行相同。只杀启动器会留下子进程，累积多了会出现多个实例抢同一个微信窗口，
表现就是「有时不回」「回了又撤回」。

## 二、开机自启

已注册计划任务 `WxAgentBotUIA`（登录时触发，失败自动重试 3 次，无执行时限）：

```powershell
Get-ScheduledTask -TaskName 'WxAgentBotUIA' | Select TaskName, State
Start-ScheduledTask -TaskName 'WxAgentBotUIA'      # 手动触发
```

脚本 `scripts\start-uia.bat` 必须是**纯 ASCII + CRLF**：cmd 按 GBK 解读 .bat，
里面写中文会让整行解析错乱（已踩过）。

登录微信不等于生效：网关进程必须在跑。但网关支持「等待模式」——
微信没启动/没登录时它不退出，扫码登录后自动接上，不用重启网关。

## 三、日志

- `logs\bot.log`：结构化日志（收到 @ / 调用工具 / 已发送）
- `logs\bot.out.log`：任务计划里的 stdout
- `logs\stdout.log` / `stderr.log`：手动启动时的重定向

排查顺序：`收到 @ 消息` → `调用工具` → `回复` → `已发送`。缺哪一环就查哪一环。

## 四、可调参数（改 `.env` 后重启网关）

| 变量 | 当前值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 便宜快；`deepseek-v4-pro` 更准但慢 2~3 倍 |
| `ENABLE_WEB_SEARCH` | `true` | 联网搜索开关 |
| `CONTEXT_MESSAGES` | 6 | 每次带多少条群消息做上下文 |
| `MAX_TOKENS` | 400 | 单次输出上限 |
| `COOLDOWN_SECONDS` | 1 | 同群两次回复最小间隔 |
| `MAX_REPLIES_PER_MINUTE` | 20 | 同群每分钟回复上限 |
| `MAX_REPLY_CHARS` | 1800 | 单条字数上限，超了分条发 |
| `UIA_POLL_INTERVAL` | 3 | 轮询间隔秒 |

省 token 的三个抓手：`CONTEXT_MESSAGES`（输入）、`MAX_TOKENS`（输出）、
以及**别让模型乱搜索**（提示词里已限定只对实时事实搜索）。

## 五、已踩过的坑（照着查）

### 微信 4.x 对 UIA 只暴露空壳
根节点是 `Qt51514QWindowIcon`、只有 2 个控件，说明 **Qt 无障碍 gate 字节是 0**。
程序里 `AccessibilityGate` 会把 `Weixin.dll+0xB39C9D8`（按版本不同，运行时扫描）
热写成 1，`mmui::` 控件树立刻物化。**微信每 5~6 秒把它重置**，所以有后台线程每 2 秒补写。

### 会话行解析
真实格式字段数不固定：
`会话名 \n [7条] \n [有人@我] \n 某人: 内容 \n 23:57`
早期把 `[有人@我]` 当摘要，导致永远判断不出 @。现在 `parse_session_name` 跳过标记字段、
摘要取时间前的最后一个候选，并单独记录 `at_me`。

### 会话已打开时会丢失未读与 @ 标记
微信在会话处于打开状态时不会保留未读数。所以判断条件不能只看 `unread>0`，
要用「摘要含 @机器人」或「有人@我」标记。

### 重复点击会话会 toggle 关闭
输入框 `Name` 在空的时候是「会话标题 + 长按鼠标 语音输入」。
用相等判断会误判未打开 → 重复点击 → 会话被关掉。必须用 `startswith`。

### 发送验证
`发送` 按钮是 `mmui::XOutlineButton`，不支持 Invoke 模式。
发送流程：粘贴 → 点按钮 → **检查输入框是否清空** → 没清空就补回车 → 再检查。

### 登录窗也叫「微信」
类名是 `mmui::LoginWindow`，面积也够大。窗口查找必须排除 `Login`，否则会绑到登录窗。

### `.tmp` 里的脚本名别撞标准库
在 `.tmp` 下建 `token.py` 会让 `tokenize` 导入失败（`token` 被遮蔽）。命名加前缀。

## 六、验证清单

改完任何东西，按这个顺序验：

```powershell
.\.venv\Scripts\python.exe run.py --mode doctor --check-api   # 配置+模型
.\.venv\Scripts\python.exe -m pytest -q                      # 47 个单测
# 然后在群里 @机器人 发一句话，看 logs\bot.log 四段日志是否齐全
```