<div align="center">

# cc-crew

### Be the boss of your Claude Code crew (Codex too), from your phone

Assign work, make the calls and approve permissions while you're away. Every employee is a real Claude Code or Codex session in your terminal, the same as if you were sitting at your desk.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776AB)
![Linux · macOS · Windows](https://img.shields.io/badge/Linux%20%C2%B7%20macOS%20%C2%B7%20Windows-supported-555)
![Feishu](https://img.shields.io/badge/Feishu-%E2%9C%93-00D6B9)

[简体中文](README.md) · **English**

<img src="docs/demo.gif" alt="cc-crew demo: tap a button on the phone, the Claude Code session on the server reacts" width="860">

<sub>Left: the Feishu group on a phone. Right: Claude Code and Codex sessions in tmux on a server. <a href="docs/demo.mp4">HD mp4</a> (an animated mock-up with made-up workers and tasks; the cards look the same as the ones cc-crew sends).</sub>

</div>

## The problem

You run several Claude Code sessions at once, each on its own task. Once you leave the desk:

- **You can't tell who's done and who's stuck.** One is waiting for you to pick A or B. Another is waiting for "Allow". They wait all afternoon.
- **New work has to wait until you're back.** You think of something on the subway and can only write it down.
- **Notifications are all or nothing.** Either you hear nothing, or your phone never stops buzzing.

cc-crew turns each Claude Code session into an employee with a badge number in a Feishu group chat. One more Claude Code session acts as your secretary. You're the boss.

## Quick start

```bash
git clone https://github.com/Mutual-Luo/cc-crew.git
cd cc-crew
python install.py          # Windows: py -3 install.py
```

1. Create a bot app on the Feishu open platform (5 minutes, [steps below](#creating-the-feishu-app)) and enter its App ID and App Secret when the installer asks.
2. Send the bot any message in Feishu, then run `cr 授权` and `cr start`. From then on it only obeys you.
3. Start sessions with `ccm` instead of `claude` (same arguments), or just run `claude` inside tmux. The group chat appears by itself.

> **Rather not do it yourself?** Paste this to your Claude Code:
> `Install cc-crew for me by following https://github.com/Mutual-Luo/cc-crew/blob/main/INSTALL.md`

## A day with cc-crew

**Morning commute.** Open Feishu and look at the pinned 📋 employee board:
- #1 finished the baseline;
- #2 is editing the paper;
- #3 🙋 needs a decision from you.

Tap 〔B〕 on #3's card, and the Claude Code session on the server carries on.

**Between meetings.** #1 wants to install a package and an approval card arrives; tap 〔允许〕 (Allow). Tell the secretary "hire someone to get repo-x's tests passing". It opens #5 in tmux and assigns the work. #5's card shows, live, what its last few steps were and how long each took. To cut in while it's busy, send `@5! don't touch the CI config` instead of waiting for it to finish the current step.

**Evening.** `@1 下班` (clock out): #1 exits cleanly, gives its number back and keeps its conversation. Tomorrow, `上班 <name>` (clock in) resumes it in the same directory with the lowest free number. At night only decisions reach you; everything else waits for you on the board.

## Features

- 🧑‍💼 **Secretary.** Plain messages go to the secretary: "clock out #3", "ask #5 how it's going", "hire someone to do X".
- 📣 **Direct orders.** `@3 …` goes to #3. Quote-reply a card to talk to that employee. `@所有人 …` (@everyone) holds a meeting.
- 🙋 **Decisions become buttons.** These include:
  - A/B/C options;
  - yes/no;
  - multi-select (tick, then submit);
  - Allow / Always allow / Deny.

  One tap answers for you. Options that come with previews (sketches, code snippets) show them, taken from the session log, so you can compare on the phone.
- ⏳ **Live progress.** The last few steps (thinking, commands, each with its duration) and the elapsed time are on the card. When the work is done, the card becomes the reply in place.
- 📋 **Employee board.** One pinned card shows who's working, idle or waiting for you. "Still waiting" updates only touch the board.
- 🔁 **Terminal and Feishu stay in sync.** Type straight into a worker's session (at the desk, over ssh or remote desktop) and Feishu still gets a live progress card that turns into the conclusion. Every message to a worker carries a source tag: 你 · 飞书 (you, Feishu), 你 · 终端 (you, terminal), `#3 name` (another worker), 助手 (relayed by the secretary) or 系统 (scheduled jobs, goal continuations); lines typed while it works are marked 插话 (cut-in). When a goal pauses on its own (check timed out, usage limit) you get a note. Claude Code sessions parked as background jobs are tracked too.
- ⏩ **Queue and send now.** Claude Code queues a message sent while it's busy until the current step is done. The card marks it 排队中 (queued); tap 〔立即发送〕 (send now) to deliver it immediately, or send `@3! …` in the first place.
- 🏷 **Tell them apart.** Each card's title carries the worker type, `CC` (Claude Code) or `Codex`, plus an install label when you have several (e.g. `Codex·官方`). The mark comes from the program actually running, never from the name.
- ✨ **Clean cards.** Title: badge, name and status. Subtitle: task and directory. What was said to the worker sits in a tinted block; the answer is the body. One row of small buttons, the rest in "···". A row of small footer tags (duration, model, context, quota) that turn orange or red when running low.
- 🌙 **Quiet.** One employee's reports within 10 minutes merge into one card, so your phone buzzes once. At night only decisions are pushed. Night hours are learned from when you usually use Claude.
- 🗂 **HR.** Clock out (conversation kept), clock in (resume in place), fire, and let employees message each other.
- 🖥 **Remote terminal.** View the screen (terminal chrome stripped; menus shown as options with buttons), press keys, stop, change effort, and run `/compact` with a progress card.
- 🔌 **Survives reboots.** After a restart you're told how long it was down. Messages you sent meanwhile are listed for you to resend or skip. One tap brings every employee back.

## Why "real Claude Code"

cc-crew doesn't call the API or run headless, and it never reads or forwards your login credentials. Employees are the interactive Claude Code sessions you start in your terminal. The bot does what you'd do at the desk: read the screen, type and press keys.

So:

- Your `CLAUDE.md`, skills, hooks, MCP servers and permission settings all keep working.
- Back at your desk, `tmux attach` and you're in the same session, with everything you did from the phone.
- Usage counts against your subscription exactly as if you'd typed it yourself.

### How it compares

| | Good for |
|---|---|
| Claude Code's official Remote Control / Channels | Continuing **one** session in the Claude app, Telegram, etc. |
| IM bridges such as cc-connect and botmux | Chatting with AI CLIs from messaging apps; they support more IMs and agents |
| Terminal multiplexers such as claude-squad | Managing several sessions while at your desk |
| **cc-crew** | Managing a **whole crew** of long-running Claude Code sessions from a Feishu group while you're away: badges, a board, a secretary, decision buttons, merged notifications and quiet hours |

They work together, for example claude-squad at the desk and cc-crew on the go.

## Codex workers

[Codex CLI](https://github.com/openai/codex) sessions running interactively in tmux are workers too: badge, board, latest answer, screen, `@3` tasks, keys, approvals, clock out / clock in. cc-crew never drives Codex headlessly (`codex exec`, app-server); it reads the screen, types, presses keys, and reads Codex's own rollout files.

- Every worker shows a type mark: `CC`, `CC·<label>` (another Claude config dir), `Codex·<label>`. The mark comes from the program actually running and its config dir, never from the name.
- Several Codex installs: `CLAUDE_MONITOR_CODEX_HOMES=label|CODEX_HOME|env-script;…` in `config.env`. Clocking in (`codex resume`) uses the worker's original install.
- Pop-ups are never answered for you: resuming a thread with a paused `/goal` shows "Resume paused goal?" (Enter would resume it). The bot does not type or press Enter while a pop-up is open: clocking in sends you the screen card with the choices, clocking out presses Esc (no choice) before `/quit`. The startup update prompt is turned off for bot-launched codex.
- Where they run: workers the bot starts or clocks back in run in its private tmux (`rtmux ls`) with `--no-daemon`, so the work itself runs in that pane's process instead of the shared per-CODEX_HOME background server, which keeps the environment of whichever terminal opened codex first (e.g. your SSH tmux).
- Hire: `cr 新建 name dir codex [codex args…]` (first install) or `codex-<label>`; the name gets a `cx-` prefix.
- Set `[tui] terminal_title = ["thread-id", "spinner", "thread-name", "project"]` in each `config.toml` so the bot can map tmux panes to sessions and see `Action Required`.
- Optional: `cr codex-hooks` installs a record-only PermissionRequest hook; trust it once with `/hooks` in Codex.
- `cr json` (and `data/crew.json`) is one agent-neutral snapshot of all workers for other clients. Each worker carries `heard`: recent messages it received and where they came from (feishu / cli / app / terminal / peer / background); the full log is `data/heard.jsonl`. Set `CR_VIA=app` when an app sends through `cr`.

## Platforms

| OS | How to start Claude so it can be controlled | Status |
|---|---|---|
| Linux | Run `claude` inside tmux, or use `ccm` instead of `claude` | Used daily |
| macOS | Same (`brew install tmux`) | Experimental |
| Windows (PowerShell / Windows Terminal / VS Code terminal, no WSL) | Use `ccm` instead of `claude` | Experimental |

Sessions started with a plain `claude` still send progress, reports and decision cards; you just can't type into them remotely. `ccm` takes exactly the same arguments and looks the same as `claude`. It runs claude in a pseudo-terminal and opens a localhost-only port with a random token, so the bot can read the screen and type.

## Creating the Feishu app

<details>
<summary>One app per computer (one app connected to two computers only delivers to one of them). Click for the steps.</summary>

Go to the [Feishu open platform](https://open.feishu.cn/app), create a custom app (企业自建应用) and add the Bot capability. Then:

- Under 权限管理 (permissions), grant these with **app identity**:
  - `im:message`: send messages as the app
  - `im:message.p2p_msg:readonly`: read direct messages sent to the bot
  - `im:message.group_msg`: read all group messages (otherwise every message must @ the bot)
  - `im:message:readonly`: read chat history (used to catch up after a restart)
  - `im:chat`: create the group and pin the board
  - `im:resource`: send and receive images and files
- Under 事件与回调 (events & callbacks), choose **long connection**. Add the event `im.message.receive_v1` and the callback `card.action.trigger`.
- Publish a version.

The installer then does the following:
1. creates a venv and installs dependencies;
2. asks for the App ID, App Secret and group name;
3. adds two hooks and a status-line command to Claude Code's `settings.json` (your original status line still shows, and the file is backed up first);
4. enables start-at-login and starts the bot.

Add `cc-crew/bin` to PATH to use `cr` and `ccm` anywhere.

</details>

## Commands

| Command | What it does |
|---|---|
| `ccm [claude args…]` | Use instead of `claude` so the bot can control the session |
| `cr start` / `cr stop` / `cr status` | Start (with a watchdog that restarts it), stop, show status and logs |
| `cr autostart on` / `off` | Start at login: the Startup folder on Windows, a LaunchAgent on macOS, a systemd user service or crontab on Linux |
| `cr 看看`, `cr @3 …`, `cr 下班 3`… | Same as saying it in Feishu; the secretary uses these |
| `cr @3! …` | Deliver right away even while it's busy, instead of queueing |
| `cr json` | One snapshot of all workers (Claude Code / Codex) for other clients; also written to `data/crew.json` |
| `cr codex-hooks` | Install a record-only PermissionRequest hook in each Codex install (optional) |

Configuration (`config.env`) is documented in the [Chinese README](README.md#常用命令).

## FAQ

**Can this get my account banned?** cc-crew only reads the screen and types for you, in the interactive Claude Code you logged into yourself. It doesn't call the API, run headless or touch credentials. We can't make any promises on Anthropic's behalf, so use it according to your own reading of the terms.

**Does it conflict with the official Remote Control?** No. Remote Control continues one session in the Claude app; cc-crew manages many from a Feishu group.

**Two computers?** Create one Feishu app and one group per computer.

**Cost?** cc-crew is free and open source. Claude Code uses your existing subscription, and Feishu custom apps are free.

**What if Feishu disconnects?** Employees keep working. Messages you sent during a short disconnect (up to 30 minutes) are replayed once the bot reconnects, and marked as caught up. After a long shutdown they're listed for you to decide.

## Security

- The bot only handles messages and button taps from one account (`FEISHU_ALLOW_OPEN_ID`). If someone else is added to the group, you're warned.
- Only you can confirm firing an employee in Feishu; the secretary can't.
- `ccm`'s control port listens on 127.0.0.1 only, with a random per-session token that stays in the local data directory.
- No identity information (Feishu name, open_id…) is ever sent to Claude.

## Roadmap

- [x] Feishu
- [ ] Lark (international Feishu)
- [ ] Slack
- [ ] Create the Feishu app by scanning a QR code

Issues and PRs welcome. If it's useful to you, a ⭐ helps others find it.

## License

MIT
