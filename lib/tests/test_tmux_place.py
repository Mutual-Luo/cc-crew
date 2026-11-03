"""招来、叫回的员工开在你平常用的 tmux 里、放回原来的窗口（crterm.place / start_tmux / close_group）的测试。
用两个临时的 tmux 服务器分别当「你平常用的 tmux」和老的私有 tmux，不碰真的。
运行：venv/bin/python lib/tests/test_tmux_place.py（只在有 tmux 的 Linux / macOS 上跑）"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

if os.name == "nt" or not shutil.which("tmux"):
    print("跳过：没有 tmux")
    sys.exit(0)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import crterm  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(HERE))
_dirs = [d for d in (os.environ.get("TMPDIR"), os.path.join(os.path.dirname(ROOT), "tmp"), "/tmp")
         if d and os.path.isdir(d) and len(d) < 60]            # tmux 的 socket 路径不能太长
TMP = tempfile.mkdtemp(prefix="cc-", dir=_dirs[0])
crterm.USER_SOCK = os.path.join(TMP, "u.sock")
crterm.PRIVATE_SOCK = os.path.join(TMP, "p.sock")
U = crterm.USER_SOCK
passed = []


def check(name, cond, detail=""):
    assert cond, f"{name} 失败 {detail}"
    passed.append(name)
    print("✓", name)


def t(*args, sock=U):
    return subprocess.run(["tmux", "-S", sock] + list(args), capture_output=True, text=True,
                          env={k: v for k, v in os.environ.items() if k != "TMUX"}).stdout.strip()


def panes():
    """{会话:窗口: (窗格, 在跑的程序, 记着的员工名)}"""
    out = {}
    for r in t("list-panes", "-a", "-F", "#{session_name}:#{window_index}\t#{pane_id}\t#{pane_current_command}\t#{@cc-crew}").splitlines():
        k, p, cmd, tag = (r.split("\t") + [""])[:4]
        out[k] = (p, cmd, tag)
    return out


def launch(name, cmd="sleep 300", label=""):
    """像启动脚本那样，在「你的 tmux」里开一个新会话（名字由 free_session 给），再 place。"""
    tmp = crterm.free_session(name)
    t("new-session", "-d", "-s", tmp, "-x", "120", "-y", "30", cmd)
    return tmp, crterm.place(tmp, name, label)


def settle(secs=0.6):
    time.sleep(secs)


os.environ.pop("TMUX", None)
try:
    # 你自己的 tmux：work 会话，0 号窗口跑着程序，4 号窗口是个闲着的 shell，5 号窗口跑着程序
    t("new-session", "-d", "-s", "work", "-x", "120", "-y", "30", "sleep 300")
    t("new-window", "-t", "work:4", "bash --norc -i")
    t("new-window", "-t", "work:5", "sleep 301")
    t("new-session", "-d", "-s", "cx-bar", "sleep 302")              # 你自己开的、正好跟员工同名的会话
    settle()
    shell4 = panes()["work:4"][0]

    # 1. work:4 来的员工：放回 work 的 4 号窗口，换掉那里的空 shell，临时会话不留
    tmp, where = launch("work:4")
    settle()
    p = panes()
    check("放回原来的窗口号", p["work:4"][1] == "sleep" and p["work:4"][2] == "work:4", p)
    check("换下来的空 shell 收掉了", shell4 not in [v[0] for v in p.values()], p)
    check("临时会话不留", not any(k.startswith(tmp + ":") for k in p), (tmp, p))
    check("说明里写了在哪", "work 的 4 号窗口" in where and "tmux attach -t work" in where, where)
    check("其他窗口没动", p["work:0"][1] == "sleep" and p["work:5"][1] == "sleep", p)

    check("没给窗口名就照 tmux 自己起的", t("display", "-p", "-t", p["work:4"][0], "#{window_name}") == "sleep")
    launch("work:9", label="codex")
    settle()
    check("窗口名改成 codex（窗口栏上认得出）", t("display", "-p", "-t", "=work:9", "#{window_name}") == "codex")

    # 2. 原窗口号被别的程序占着：排到这个会话最后，不动那个窗口
    _, where = launch("work:5")
    settle()
    p = panes()
    check("窗口被占着就排到最后", p["work:5"][2] == "" and any(k.startswith("work:") and v[2] == "work:5" and k != "work:5"
                                                         for k, v in p.items()), p)

    # 3. 原窗口号没有了：就开在那个窗口号
    launch("work:7")
    settle()
    check("窗口号空着就用它", panes().get("work:7", ("", "", ""))[2] == "work:7", panes())

    # 4. 普通名字：自己一个会话；跟你自己的会话重名就带 -2，你的那个不动
    tmp, where = launch("cx-foo")
    settle()
    p = panes()
    check("普通名字自己一个会话", p.get("cx-foo:0", ("", "", ""))[2] == "cx-foo", p)
    tmp, _ = launch("cx-bar")
    settle()
    p = panes()
    check("重名不动你的会话", p["cx-bar:0"][1] == "sleep" and p["cx-bar:0"][2] == "", p)
    check("重名的员工带 -2", tmp == "cx-bar-2" and p.get("cx-bar-2:0", ("", "", ""))[2] == "cx-bar", (tmp, p))

    # 5. 下班后留下空 shell，再上班：回到同一个位置，不多开会话
    t("respawn-pane", "-k", "-t", panes()["cx-foo:0"][0], "bash --norc -i")   # 员工退出后留下的 shell（窗格上的名字还在）
    settle()
    check("running：只剩 shell 不算在跑", crterm.running("cx-foo") == "", panes())
    tmp, where = launch("cx-foo")
    settle()
    p = panes()
    check("再上班回到原来的会话", tmp == "cx-foo-2" and p["cx-foo:0"][1] == "sleep" and p["cx-foo:0"][2] == "cx-foo"
          and not any(k.startswith("cx-foo-2:") for k in p), (tmp, p))
    check("find_tagged 找到最新的", crterm.find_tagged("cx-foo") == (U, p["cx-foo:0"][0]), crterm.find_tagged("cx-foo"))

    # 6. 上次的窗格你拿去跑别的了：不再算它的，员工另开
    t("respawn-pane", "-k", "-t", p["work:4"][0], "sleep 303")
    settle()
    tmp, _ = launch("work:4")
    settle()
    p = panes()
    check("你在用的窗格不抢", p["work:4"][1] == "sleep" and p["work:4"][2] == "", p)
    check("员工另开了一个窗口", sum(v[2] == "work:4" for v in p.values()) == 1, p)

    # 7. running：claude 还在跑就不重开
    fake = os.path.join(TMP, "claude")
    os.symlink(shutil.which("sleep"), fake)
    tmp, _ = launch("cx-run", f"{fake} 300")
    settle()
    check("claude 还在跑", crterm.running("cx-run") == "claude", panes())
    ok, why = crterm.start_tmux("cx-run", TMP, ["sleep", "300"], dict(os.environ))
    check("claude 还在跑就不开", not ok and "还在运行" in why, why)
    launch("cx-inner", f"bash -c '{fake} 300; true'")              # 像 claude-inner.sh：前台显示 bash，claude 是它的子进程
    settle()
    check("经脚本跑的也认得出", panes()["cx-inner:0"][1] == "bash" and crterm.running("cx-inner") == "claude", panes())

    # 8. start_tmux：跟本进程不同的环境变量带进新窗格，去掉的也去掉
    out = os.path.join(TMP, "env.txt")
    os.environ["CC_TEST_DROP"] = "old"
    env = {**{k: v for k, v in os.environ.items() if k != "CC_TEST_DROP"}, "CC_TEST_ADD": "new"}
    t("set-environment", "-g", "CC_TEST_DROP", "old")                 # tmux 自己的环境里有的变量
    ok, where = crterm.start_tmux("cx-env", TMP, ["sh", "-c", f'echo "add=$CC_TEST_ADD drop=$CC_TEST_DROP" > {out}; sleep 300'], env)
    for _ in range(30):
        if os.path.exists(out) and os.path.getsize(out):
            break
        time.sleep(0.1)
    got = open(out).read().strip() if os.path.exists(out) else ""
    check("start_tmux 开好并放好", ok and "cx-env" in where, where)
    check("新变量带进去、去掉的去掉", got == "add=new drop=", got)

    # 9. close_group：只收员工自己那个空 shell 窗格，不动你会话里别的窗口
    t("respawn-pane", "-k", "-t", panes()["work:7"][0], "bash --norc -i")
    settle()
    s = {"term": "tmux", "socket": U, "pane": panes()["work:7"][0], "group": "work", "tag": "work:7", "private": True}
    crterm.close_group(s)
    settle(0.3)
    p = panes()
    check("close_group 只收自己的窗格", "work:7" not in p and "work:0" in p and "work:5" in p, p)
    s = {"term": "tmux", "socket": U, "pane": p["work:0"][0], "group": "work", "tag": "", "private": False}
    crterm.close_group(s)
    check("没记名字的不收", "work:0" in panes(), panes())

    # 9b. 你自己开的员工（名字不带窗口号，比如 exp-a 在 0 号窗口）：按登记里记的原会话、原窗口放回去，不另开会话
    t("new-session", "-d", "-s", "mine", "-x", "120", "-y", "30", "bash --norc -i")   # 它 /exit 以后，你原来那个 shell 还在
    t("new-window", "-t", "mine:1", "sleep 304")
    settle()
    tmp = crterm.free_session("mine")
    t("new-session", "-d", "-s", tmp, "-x", "120", "-y", "30", "sleep 305")
    where = crterm.place(tmp, "mine", "claude", ("mine", "0"))
    settle()
    p = panes()
    check("回到原来的会话和窗口（不是另开 mine-2）", p["mine:0"][2] == "mine" and p["mine:0"][1] == "sleep" and tmp == "mine-2"
          and not any(k.startswith("mine-2:") for k in p) and p["mine:1"][1] == "sleep", (tmp, p))

    # 10. 会话名前缀（共用机器上别人一看就知道是你的）：新开的会话带上，员工名不变；已经带了的不重复加
    crterm.PREFIX = "me-"
    tmp, where = launch("cx-pre")
    settle()
    p = panes()
    check("新会话名带前缀", tmp == "me-cx-pre" and p.get("me-cx-pre:0", ("", "", ""))[2] == "cx-pre", (tmp, p))
    check("已经带前缀的不重复加", crterm.free_session("me-x") == "me-x")
    tmp, _ = launch("work:8")
    settle()
    check("放回你会话的窗口不受前缀影响", panes().get("work:8", ("", "", ""))[2] == "work:8", panes())
    crterm.PREFIX = ""

    # 11. 老的启动脚本还开在私有 tmux 里：不挪，只记名字，after_launch 照样认得
    t("new-session", "-d", "-s", "old-style", "sleep 300", sock=crterm.PRIVATE_SOCK)
    where = crterm.place("old-style", "old-style")
    tag = t("display", "-p", "-t", "=old-style:", "#{@cc-crew}", sock=crterm.PRIVATE_SOCK)
    check("私有 tmux 里的只记名字", where == "" and tag == "old-style", (where, tag))
    check("find_tagged 也找私有 tmux", (crterm.find_tagged("old-style") or ("",))[0] == crterm.PRIVATE_SOCK)
finally:
    for sock in (U, crterm.PRIVATE_SOCK):
        t("kill-server", sock=sock)
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n全部通过：{len(passed)} 项")
