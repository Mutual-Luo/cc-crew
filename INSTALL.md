# Installing cc-crew (instructions for Claude Code)

You are helping the user install cc-crew. It lets them manage their Claude Code sessions from a Feishu group chat. Talk to the user in their language, and go one step at a time. Never print, echo or log the Feishu App Secret.

## 1. Check prerequisites

- Python 3.9 or newer (`python3 --version`; on Windows `py -3 --version`).
- Claude Code is installed and the user is logged in.
- Linux / macOS: `tmux` is installed. If it isn't, tell the user how to install it (`sudo apt install tmux`, `brew install tmux`) and wait for them. Windows needs no tmux.

## 2. Get the code

Ask the user where to put it (default: their home directory), then:

```bash
git clone https://github.com/Mutual-Luo/cc-crew.git
cd cc-crew
```

## 3. The user creates a Feishu app

This part happens in the browser, so the user does it. Show them these steps and wait until they say it's done:

1. Open https://open.feishu.cn/app, then 创建企业自建应用. Add the 机器人 capability.
2. Go to 权限管理. Grant these scopes with **app identity** (应用身份):
   - `im:message`
   - `im:message.p2p_msg:readonly`
   - `im:message.group_msg`
   - `im:message:readonly`
   - `im:chat`
   - `im:resource`
3. Go to 事件与回调. Choose **长连接** (long connection). Add the event `im.message.receive_v1` and the callback `card.action.trigger`.
4. Publish a version (创建版本 → 发布).

## 4. Write the config without seeing the secret

Create `config.env` in the cc-crew directory with the App ID, which the user can tell you because it isn't secret, and a placeholder for the secret:

```
FEISHU_APP_ID=cli_xxxxxxxx
FEISHU_APP_SECRET=PASTE_SECRET_HERE
FEISHU_GROUP_NAME=我的Claude员工们-1群
```

Ask the user to open `config.env` in their editor and replace `PASTE_SECRET_HERE` with the App Secret themselves. Do not ask them to paste the secret into the chat. Afterwards, check that the line no longer contains the placeholder, without printing the line:

```bash
grep -q PASTE_SECRET_HERE config.env && echo "secret not filled in yet" || echo "ok"
```

Optional keys are listed in README.md under 配置项. Examples are `CLAUDE_MONITOR_FILES_ROOT` (the only directory employees may work in) and `CLAUDE_MONITOR_PROXY`.

## 5. Install

```bash
python3 install.py --yes        # Windows: py -3 install.py --yes
```

`--yes` uses `config.env` and the defaults without asking questions. It does the following:
- creates a venv and installs dependencies;
- adds two hooks and a status line to Claude Code's `settings.json`, after backing it up to `settings.json.bak-cc-crew`;
- turns on start-at-login;
- starts the bot.

Tell the user that the install changes `settings.json`, before you run it.

## 6. Bind the owner

Ask the user to send any message to the bot in Feishu (search for the app's name). Then run:

```bash
bin/cr 授权
bin/cr start
```

After that, the bot only obeys that one Feishu account. Within a minute a group chat named after `FEISHU_GROUP_NAME` appears in Feishu.

## 7. Tell the user how to use it

- Add `cc-crew/bin` to PATH, so that `cr` and `ccm` work in any terminal.
- To start a session the bot can control, use `ccm` instead of `claude` (same arguments). On Linux and macOS, running `claude` inside tmux also works.
- In the Feishu group:
  - Typing talks to the secretary (助手).
  - `@3 内容` talks to employee #3.
  - Buttons on cards answer questions and approve permissions.
- `bin/cr status` shows whether the bot is running.
