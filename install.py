"""cc-crew 一键安装（Windows / macOS / Linux）：

    python install.py            装好、填飞书凭证、接上 Claude Code、设开机自启、启动
    python install.py --no-start 只装不启动
    python install.py --no-autostart  不设开机自启

做的事（都只在安装目录和 Claude Code 的设置文件里）：
1. 在安装目录建 venv，装依赖（lark-oapi、psutil、pyte；Windows 再加 pywinpty）
2. 问你飞书应用的 App ID / App Secret 等，写到 config.env（只有你能读）
3. 在 Claude Code 的 settings.json 里加两个 hook（记录「它在等你」「它要权限」）和状态栏命令
   （你原来的状态栏照常显示）；改之前先备份成 settings.json.bak-cc-crew
4. 设置开机（登录）自动启动，然后启动机器人
"""
import getpass
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
IS_WIN = os.name == "nt"
VENV = os.path.join(ROOT, "venv")
VPY = os.path.join(VENV, "Scripts", "python.exe") if IS_WIN else os.path.join(VENV, "bin", "python")
CONFIG = os.path.join(ROOT, "config.env")
REQUIREMENTS = ["lark-oapi", "psutil", "pyte"] + (["pywinpty"] if IS_WIN else [])


def say(msg):
    print(msg, flush=True)


def ask(prompt, default="", secret=False):
    shown = f"{prompt}" + (f"（回车用 {default}）" if default and not secret else "") + "："
    val = (getpass.getpass(shown) if secret else input(shown)).strip()
    return val or default


def short_path(path):
    """Windows：路径里有空格时换成 8.3 短路径（C:\\Users\\JOHNSM~1\\…），hook 命令在 Git Bash 和 PowerShell 里都能跑。"""
    path = path.replace("\\", "/")
    if not IS_WIN or " " not in path:
        return path
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    if ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024) and " " not in buf.value:
        return buf.value.replace("\\", "/")
    raise SystemExit(f"安装路径里有空格（{path}），而且拿不到短路径。请把 cc-crew 放到没有空格的目录再装。")


def read_config():
    env = {}
    for name in ("feishu.env", "config.env"):
        try:
            for line in open(os.path.join(ROOT, name), encoding="utf-8"):
                k, sep, v = line.strip().partition("=")
                if sep and not k.startswith("#"):
                    env[k.strip()] = v.strip()
        except OSError:
            pass
    return env


def write_config(env):
    lines = ["# cc-crew 配置。这个文件里有飞书密钥，不要分享。"]
    lines += [f"{k}={v}" for k, v in env.items() if v != ""]
    with open(CONFIG + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(CONFIG + ".tmp", CONFIG)
    if not IS_WIN:
        os.chmod(CONFIG, 0o600)


def step_venv():
    if sys.version_info < (3, 9):
        raise SystemExit("需要 Python 3.9 或更新的版本")
    if not os.path.exists(VPY):
        say("· 建 venv …")
        subprocess.run([sys.executable, "-m", "venv", VENV], check=True)
    say("· 装依赖：" + " ".join(REQUIREMENTS))
    subprocess.run([VPY, "-m", "pip", "install", "-q", "--upgrade", "-r", os.path.join(ROOT, "requirements.txt")], check=True)


def step_config():
    env = read_config()
    say("\n飞书机器人（开放平台 open.feishu.cn → 创建企业自建应用 → 凭证与基础信息）。已经填过的直接回车。")
    env["FEISHU_APP_ID"] = ask("App ID", env.get("FEISHU_APP_ID", ""))
    secret = ask("App Secret（输入时不显示）" + ("，回车保留原来的" if env.get("FEISHU_APP_SECRET") else ""), secret=True)
    env["FEISHU_APP_SECRET"] = secret or env.get("FEISHU_APP_SECRET", "")
    env["FEISHU_GROUP_NAME"] = ask("员工群的名字（每台电脑一个群，第二台可以叫 …-2群）",
                                   env.get("FEISHU_GROUP_NAME", "我的Claude员工们-1群"))
    env["CLAUDE_MONITOR_FILES_ROOT"] = ask("员工只能在哪个目录下干活（也只把这下面的文件发到手机）",
                                           env.get("CLAUDE_MONITOR_FILES_ROOT", os.path.expanduser("~")))
    env["CLAUDE_MONITOR_PROXY"] = ask("claude 必须经过的代理「主机:端口」（没有就直接回车）", env.get("CLAUDE_MONITOR_PROXY", ""))
    if os.environ.get("CLAUDE_CONFIG_DIR") and not env.get("CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = os.environ["CLAUDE_CONFIG_DIR"]
    write_config(env)
    say(f"· 配置写到了 {CONFIG}")
    return env


def step_claude_settings(env):
    cdir = env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    path = os.path.join(cdir, "settings.json")
    os.makedirs(cdir, exist_ok=True)
    try:
        settings = json.load(open(path, encoding="utf-8"))
    except FileNotFoundError:
        settings = {}
    except ValueError:
        raise SystemExit(f"{path} 不是合法的 JSON，先修好它再装")
    if os.path.exists(path) and not os.path.exists(path + ".bak-cc-crew"):
        shutil.copy2(path, path + ".bak-cc-crew")
    py, root = short_path(VPY), short_path(ROOT)
    hook_cmd = f"{py} -S {root}/bin/cr-hook"              # -S：不加载第三方包，启动快
    status_cmd = f"{py} -S {root}/bin/cr-statusline"
    hooks = settings.setdefault("hooks", {})
    for event in ("Notification", "PermissionRequest"):
        groups = hooks.setdefault(event, [])
        mine = [h for g in groups for h in g.get("hooks", []) if "cr-hook" in h.get("command", "")]
        if mine:
            for h in mine:
                h["command"] = hook_cmd
        else:
            groups.append({"hooks": [{"type": "command", "command": hook_cmd}]})
    old = (settings.get("statusLine") or {}).get("command", "")
    if old and "cr-statusline" not in old:               # 你原来的状态栏：交给 cr-statusline 照常显示
        os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
        with open(os.path.join(ROOT, "data", "statusline-orig.txt"), "w", encoding="utf-8") as f:
            f.write(old + "\n")
    settings["statusLine"] = {**(settings.get("statusLine") or {}), "type": "command", "command": status_cmd}
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    os.replace(path + ".tmp", path)
    say(f"· 已在 {path} 加上 hook 和状态栏（原文件备份在 settings.json.bak-cc-crew）")


def step_start(start):
    cli = [VPY, os.path.join(ROOT, "lib", "crcli.py")]
    if "--no-autostart" not in sys.argv and ask("开机（登录）自动启动机器人？[Y/n]", "y").lower().startswith("y"):
        say("· " + subprocess.run(cli + ["autostart", "on"], capture_output=True, text=True, encoding="utf-8").stdout.strip())
    if start:
        say("· " + subprocess.run(cli + ["start"], capture_output=True, text=True, encoding="utf-8").stdout.strip())


def main():
    say(f"安装 cc-crew 到 {ROOT}")
    step_venv()
    env = step_config()
    step_claude_settings(env)
    step_start("--no-start" not in sys.argv)
    bin_dir = os.path.join(ROOT, "bin")
    say(f"""
装好了。接下来：
1. 把 {bin_dir} 加进 PATH，以后在终端里就能用 cr、ccm。
2. 以后开 Claude 用 ccm 代替 claude（参数一样），机器人才能替你打字；直接打 claude 开的只能看不能遥控。
   Linux / macOS 上在 tmux 里直接跑 claude 也行。
3. 在飞书里给机器人发一句话，然后运行：cr 授权（只认你一个人），再 cr start。
4. 等一会儿，飞书里会出现「{env.get('FEISHU_GROUP_NAME')}」，在群里跟助手说话就行。""")


if __name__ == "__main__":
    main()
