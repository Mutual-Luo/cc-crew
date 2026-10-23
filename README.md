# cc-crew

**Your Claude Code crew. You're the boss.** Run every Claude Code session on your machines as an employee — assign work, approve, review and hire/fire right from a Feishu group chat on your phone. (Lark support is on the way.)

**在飞书里当老板，管你电脑上的一群 Claude Code。**

你在电脑或服务器上同时开着好几个 Claude Code，各干各的活。出门以后想知道谁干完了、谁卡在等你点「允许」、谁在问你选 A 还是 B，还想顺手派个新活，就用它。

- 飞书里自动建一个「员工群」。你是老板，每个 Claude Code 会话是一个有工号的员工，另有一个**助手**（也是 Claude Code）当秘书。
- 在群里直接打字是跟秘书说：「让 3 号下班」「问问 5 号进展」「招个人把这个仓库的测试跑通」。秘书会去办。
- 「@3 内容」直接派活给 3 号；引用某个员工的卡片回复，就是跟它说话。
- 员工要你拍板时发 🙋 卡片，A/B/C 选项、「做不做」和「允许 / 不允许」都做成按钮，点一下就替你回了；干完了发 📝 汇报。只说「还在等」的不打扰你，只更新置顶的 📋 员工看板。
- 不吵：同一个员工 10 分钟内的汇报合成一张卡片（手机只响一次）；夜里你没在用时只推要你拍板的，其余攒着，看板上点一下再看。几点算夜里按你平时几点用 Claude 自动算，不用设。
- 卡片上有实时进度、模型、上下文用量、5 小时和每周额度。也能看屏幕、按键、停止、改思考程度、压缩上下文。
- 下班（正常退出，对话记录留着）、上班（在原目录接着原来的对话）、开除、员工之间互相传话。
- 电脑关机再开：告诉你停了多久；关机期间你发的消息列出来，由你决定要不要重发；断掉的员工一键全部上班。

**全程用的是你自己在终端里跑的交互式 Claude Code**，跟你坐在电脑前打字一样。不走 API，没有无头模式，也不转发你的账号。

## 支持的系统

| 系统 | 怎么开 Claude 才能被遥控 | 状态 |
|---|---|---|
| Linux | 在 tmux 里跑 `claude`，或者用 `ccm` 代替 `claude` | 天天在用 |
| macOS | 同上（tmux 用 `brew install tmux` 装） | 实验性 |
| Windows（PowerShell / Windows Terminal / VS Code 终端，不用 WSL） | 用 `ccm` 代替 `claude` | 实验性 |

直接打 `claude` 开的会话也能收到进展、汇报和拍板卡片，只是不能远程打字。`ccm` 的参数和 `claude` 完全一样，界面也一样：它在伪终端里跑 claude，同时留一个只有本机能连、带随机口令的端口，给机器人读屏和打字。

## 安装

**1. 建一个飞书应用**（每台电脑一个应用：同一个应用连两台电脑，消息只会推给其中一台）

到 [飞书开放平台](https://open.feishu.cn/app) → 创建企业自建应用 → 添加「机器人」能力。然后：

- 权限管理里，用**应用身份**开通：
  - `im:message`：以应用身份发消息
  - `im:message.p2p_msg:readonly`：读取用户发给机器人的单聊消息
  - `im:message.group_msg`：获取群组中所有消息（不开的话，群里每句话都得 @ 机器人）
  - `im:message:readonly`：获取单聊、群组消息（机器人重启时补收用）
  - `im:chat`：创建员工群、置顶看板
  - `im:resource`：收发图片和文件
- 事件与回调：订阅方式选**长连接**；事件加 `im.message.receive_v1`，回调加 `card.action.trigger`。
- 发布一个版本。

**2. 装 cc-crew**（需要 Python 3.9+，并且已经装好、登录了 Claude Code）

```bash
git clone https://github.com/Mutual-Luo/cc-crew.git
cd cc-crew
python install.py        # Windows：py -3 install.py
```

安装程序会：
1. 建 venv，装依赖；
2. 问你 App ID、App Secret 和群名；
3. 在 Claude Code 的 `settings.json` 里加两个 hook 和状态栏命令（你原来的状态栏照常显示，原文件先备份）；
4. 设好开机自启，然后启动机器人。

**3. 认主人**：在飞书里给机器人发一句话，然后运行 `cr 授权`，再运行 `cr start`。从此机器人只听你一个人的。过一会儿飞书里会出现员工群。

把 `cc-crew/bin` 加进 PATH，以后用 `ccm` 开 Claude 就行。

## 常用命令

| 命令 | 作用 |
|---|---|
| `ccm [claude 参数…]` | 代替 `claude`，机器人能遥控它 |
| `cr start` / `cr stop` / `cr status` | 启动（带看门狗，挂了自动重启）/ 停止 / 看状态和日志 |
| `cr autostart on` / `off` | 开机自启：Windows 用启动文件夹，macOS 用 LaunchAgent，Linux 用 systemd 用户服务或 crontab |
| `cr 看看`、`cr @3 内容`、`cr 下班 3`… | 和在飞书里说的一样，助手就是用这些命令干活的 |

## 配置（`config.env`，一行一个 `KEY=VALUE`）

| 配置项 | 说明 |
|---|---|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书应用凭证 |
| `FEISHU_ALLOW_OPEN_ID` | 唯一允许的飞书账号（`cr 授权` 自动填） |
| `FEISHU_GROUP_NAME` | 员工群名字；第二台电脑可以叫「…-2群」 |
| `CLAUDE_CONFIG_DIR` | Claude Code 的配置目录（默认 `~/.claude`） |
| `CLAUDE_MONITOR_FILES_ROOT` | 员工只能在这个目录下干活，也只把这下面的文件发到手机（默认家目录） |
| `CLAUDE_MONITOR_PROXY` | `主机:端口`。设了以后，claude 必须经这个代理上网，代理不通就不启动 |
| `CLAUDE_MONITOR_LAUNCH_MODE` | 新开员工的方式：`tmux`（服务器，看不见但一直在）或 `window`（桌面上弹一个终端窗口）。默认 Linux 用 tmux，macOS 和 Windows 用 window |
| `CLAUDE_MONITOR_LAUNCH_SCRIPT` | 自己的安全启动脚本，参数是「会话名 目录 claude参数…」，见 `examples/` |
| `CLAUDE_MONITOR_QUIET_PREFIXES` | 自动任务会话的名字前缀（逗号分隔），这类会话停下、退出都不报警 |
| `CLAUDE_MONITOR_QUIET_HOURS` | 夜间免打扰：默认 `auto`（按你最近三周几点跟 Claude 说话算），也可以写 `1-8`，或 `off` 关掉 |

本机专用的说明可以写进 `assistant-prompt.local.md`，助手每次启动时会附在说明书后面。

## 安全

- 机器人只处理 `FEISHU_ALLOW_OPEN_ID` 这一个人的消息和按钮点击；群里被拉进别人会提醒你。
- 开除员工只能你自己在飞书里点确认，助手做不到。
- `ccm` 的控制端口只监听 127.0.0.1，每个会话一个随机口令，口令只存在本机的数据目录里。
- 不往你的 Claude 发任何身份信息（飞书昵称、open_id 等）。

## 卸载

`cr autostart off && cr stop`，然后把 `settings.json` 里含 `cr-hook`、`cr-statusline` 的那几项删掉（或者用安装时备份的 `settings.json.bak-cc-crew` 恢复），再删掉这个目录。

## 许可证

MIT
