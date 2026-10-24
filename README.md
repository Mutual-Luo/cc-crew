<div align="center">

# cc-crew

### 在飞书里当老板，管你电脑上的一群 Claude Code

出门也能派活、拍板、点允许。员工都是你终端里真的 Claude Code，跟坐在电脑前一样。

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776AB)
![Linux · macOS · Windows](https://img.shields.io/badge/Linux%20%C2%B7%20macOS%20%C2%B7%20Windows-supported-555)
![Feishu](https://img.shields.io/badge/%E9%A3%9E%E4%B9%A6-%E2%9C%93-00D6B9)

**简体中文** · [English](README.en.md)

<img src="docs/demo.gif" alt="cc-crew 演示：手机上点按钮，服务器上的 Claude Code 跟着动" width="860">

<sub>左边是手机上的飞书群，右边是服务器上 tmux 里的 4 个 Claude Code。<a href="docs/demo.mp4">高清 mp4</a>（演示动画，卡片样式和 cc-crew 发出的一样）</sub>

</div>

## 它解决什么

你同时开着好几个 Claude Code，各干各的活。一离开电脑：

- **不知道谁干完了、谁卡住了。** 有的在等你选 A 还是 B，有的在等你点「允许」，一等就是一下午。
- **想派个新活，得回到电脑前。** 地铁上、会议间隙想到一件事，只能先记着。
- **通知太多又太少。** 要么什么都不知道，要么手机响个不停。

cc-crew 把每个 Claude Code 会话变成飞书群里一个有工号的员工，另有一个 Claude Code 当秘书，你在群里当老板。

## 30 秒上手

```bash
git clone https://github.com/Mutual-Luo/cc-crew.git
cd cc-crew
python install.py          # Windows：py -3 install.py
```

1. 先在飞书开放平台建一个机器人应用（5 分钟，[步骤见下面](#飞书应用怎么建)），安装时填上它的 App ID 和 App Secret。
2. 在飞书里给机器人发一句话，再运行 `cr 授权`、`cr start`。从此它只听你一个人的。
3. 以后用 `ccm` 代替 `claude` 开会话（参数一样），或者直接在 tmux 里跑 `claude`。员工群会自动出现。

> **懒得自己装？** 把这句话发给你的 Claude Code：
> `按 https://github.com/Mutual-Luo/cc-crew/blob/main/INSTALL.md 帮我装 cc-crew`

## 一天是这样的

**早上通勤**　打开飞书，置顶的 📋 员工看板：1 号跑完基线了，2 号在改论文，3 号🙋等你拍板。点 3 号卡片上的〔B〕，服务器上那个 Claude Code 立刻接着干。

**开会间隙**　1 号要装个包，弹来 🔐 卡片，点〔允许这一次〕。跟秘书说一句「招个人把 repo-x 的测试跑通」，秘书在 tmux 里开好 5 号、派好活。5 号的卡片实时显示它读了哪个文件、跑了哪条命令、已经干了多久。

**晚上**　「@1 下班」：1 号正常退出，对话记录留着，明天「上班 1」在原目录接着干。夜里只推要你拍板的，其余攒着，看板上点一下再看。

## 功能

- 🧑‍💼 **秘书**：直接打字就是跟秘书说。「让 3 号下班」「问问 5 号进展」「招个人干 X」，秘书去办。
- 📣 **直接派活**：「@3 内容」发给 3 号；引用谁的卡片回复就是跟谁说；「@所有人 …」开会。
- 🙋 **拍板做成按钮**：A/B/C 选项、「做不做」、多选题（勾选再提交）、「允许 / 不允许」，点一下就替你回了。
- ⏳ **实时进度**：像在电脑前看着它干活，每一步、计时、思考程度、上下文和 5 小时 / 每周额度都在卡片上。做完原地变成回复。
- 📋 **员工看板**：置顶一张，谁在干活、谁空闲、谁等你，一眼看完。只说「还在等」的只更新看板，不单独打扰你。
- 🌙 **不吵**：同一个员工 10 分钟内的汇报合成一张，手机只响一次；夜里只推要你拍板的。几点算夜里按你平时几点用 Claude 自动算。
- 🗂 **人事**：下班（对话留着）、上班（原目录接着原对话）、开除、员工之间互相传话。
- 🖥 **遥控终端**：看屏幕、按键、停止、改思考程度、`/compact`（带进度）、`/model` 菜单变按钮。
- 🔌 **断电不慌**：电脑重启后告诉你停了多久，期间你发的消息列出来由你决定要不要重发，断掉的员工一键全部上班。

## 为什么是「真的 Claude Code」

cc-crew 不调 API，也不跑无头模式，不读、不转发你的登录凭证。员工就是你在终端里开的交互式 Claude Code。机器人做的事和你坐在电脑前一样：看屏幕、打字、按键。

所以：

- 你的 `CLAUDE.md`、技能、hook、MCP、权限设置全都照常生效。
- 回到电脑前，`tmux attach` 就能接着用同一个会话，手机上做过的事都在。
- 订阅额度怎么算，跟你自己用一样。

### 和别的方案比

| | 适合 |
|---|---|
| Claude Code 官方 Remote Control / Channels | 在 Claude App 或 Telegram 等里接着用**某一个**会话 |
| cc-connect、botmux 等 IM 桥接 | 在聊天软件里跟 AI CLI 对话，支持的 IM 和 Agent 更多 |
| claude-squad 等终端多开工具 | 坐在电脑前管理多个会话 |
| **cc-crew** | 不在电脑前时，在飞书群里**同时管一群**长期运行的 Claude Code：工号、看板、秘书、拍板按钮、合并通知、夜间免打扰 |

它们可以一起用，比如电脑前用 claude-squad，出门用 cc-crew。

## 支持的系统

| 系统 | 怎么开 Claude 才能被遥控 | 状态 |
|---|---|---|
| Linux | 在 tmux 里跑 `claude`，或者用 `ccm` 代替 `claude` | 天天在用 |
| macOS | 同上（`brew install tmux`） | 实验性 |
| Windows（PowerShell / Windows Terminal / VS Code 终端，不用 WSL） | 用 `ccm` 代替 `claude` | 实验性 |

直接打 `claude` 开的会话也能收到进展、汇报和拍板卡片，只是不能远程打字。`ccm` 的参数和界面跟 `claude` 完全一样：它在伪终端里跑 claude，同时留一个只有本机能连、带随机口令的端口，给机器人读屏和打字。

## 飞书应用怎么建

<details>
<summary>每台电脑一个应用（同一个应用连两台电脑，消息只会推给其中一台）。点开看步骤</summary>

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

安装程序会：建 venv、装依赖；问你 App ID、App Secret 和群名；在 Claude Code 的 `settings.json` 里加两个 hook 和状态栏命令（你原来的状态栏照常显示，原文件先备份）；设好开机自启并启动机器人。

把 `cc-crew/bin` 加进 PATH，以后就能用 `cr`、`ccm`。

</details>

## 常用命令

| 命令 | 作用 |
|---|---|
| `ccm [claude 参数…]` | 代替 `claude`，机器人能遥控它 |
| `cr start` / `cr stop` / `cr status` | 启动（带看门狗，挂了自动重启）/ 停止 / 看状态和日志 |
| `cr autostart on` / `off` | 开机自启：Windows 用启动文件夹，macOS 用 LaunchAgent，Linux 用 systemd 用户服务或 crontab |
| `cr 看看`、`cr @3 内容`、`cr 下班 3`… | 和在飞书里说的一样，秘书就是用这些命令干活的 |

<details>
<summary>配置项（<code>config.env</code>，一行一个 <code>KEY=VALUE</code>）</summary>

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

本机专用的说明可以写进 `assistant-prompt.local.md`，秘书每次启动时会附在说明书后面。

</details>

## 常见问题

**会不会被封号？**　cc-crew 只是替你看屏幕、打字，用的是你自己登录的交互式 Claude Code，不调 API、不跑无头模式、不碰登录凭证。但我们没法替 Anthropic 做任何保证，请按你自己对服务条款的理解使用。

**和官方 Remote Control 冲突吗？**　不冲突。官方的是在 Claude App 里接着用某一个会话；cc-crew 是在飞书群里同时管一群。

**两台电脑怎么办？**　每台电脑建一个飞书应用、一个员工群（「…-1群」「…-2群」）。

**要花钱吗？**　cc-crew 免费开源。Claude Code 照你原来的订阅算，飞书自建应用不收费。

**飞书断了会怎样？**　员工照常干活。断线那一会儿你发的消息（30 分钟以内的），机器人连上后自动补上，并告诉你是补收的；电脑关机很久的，会列出来由你决定要不要重发。

## 安全

- 机器人只处理 `FEISHU_ALLOW_OPEN_ID` 这一个人的消息和按钮点击；有别人被拉进群会提醒你。
- 开除员工只能你自己在飞书里点确认，秘书做不到。
- `ccm` 的控制端口只监听 127.0.0.1，每个会话一个随机口令，口令只存在本机的数据目录里。
- 不往你的 Claude 发任何身份信息（飞书昵称、open_id 等）。

## 路线图

- [x] 飞书
- [ ] Lark（飞书国际版）
- [ ] Slack
- [ ] 扫码一键建飞书应用

欢迎提 Issue 和 PR。觉得有用的话，点个 ⭐ 让更多人看到。

## 卸载

`cr autostart off && cr stop`，然后把 `settings.json` 里含 `cr-hook`、`cr-statusline` 的那几项删掉（或者用安装时备份的 `settings.json.bak-cc-crew` 恢复），再删掉这个目录。

## 许可证

MIT
