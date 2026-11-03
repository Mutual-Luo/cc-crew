"""客户端自己更新以后，闲着的员工换上新版本（crcore.outdated / free_to_restart / Remote.upgrade_tick），
以及主动下班的不报「退出了」。用临时目录假装装了两个版本的 claude，起真进程看能不能认出谁在跑旧版；重开本身用假的。
运行：venv/bin/python lib/tests/test_upgrade.py（只在 Linux 上跑：要读 /proc/<pid>/exe；不碰真的数据，不启动真的 claude）"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

if not os.path.isdir("/proc/self"):
    print("跳过：没有 /proc")
    sys.exit(0)

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.dirname(HERE)
TMP = tempfile.mkdtemp(prefix="cr-upgrade-")
CLAUDE_DIR = os.path.join(TMP, "claude")
VERSIONS = os.path.join(TMP, "versions")
BIN = os.path.join(TMP, "bin")
for d in (os.path.join(CLAUDE_DIR, "sessions"), VERSIONS, BIN, os.path.join(TMP, "data")):
    os.makedirs(d)
os.environ.update(CLAUDE_CONFIG_DIR=CLAUDE_DIR, CLAUDE_MONITOR_DATA=os.path.join(TMP, "data"), CLAUDE_MONITOR_PROXY="",
                  CLAUDE_MONITOR_LAUNCH_SCRIPT="", CLAUDE_MONITOR_QUIET_PREFIXES="资讯")
for v in ("2.1.300", "2.1.317"):                         # 两个版本的「claude」：其实都是 sleep
    shutil.copy(shutil.which("sleep"), os.path.join(VERSIONS, v))
os.symlink(os.path.join(VERSIONS, "2.1.317"), os.path.join(BIN, "claude"))   # claude 命令指向新版（自己更新过了）
os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]
sys.path.insert(0, LIB)
import crcore  # noqa: E402

passed, procs = [], []


def check(name, cond, detail=""):
    assert cond, f"{name} 失败 {detail}"
    passed.append(name)
    print("✓", name)


def run(*argv):
    p = subprocess.Popen(list(argv), start_new_session=True)   # 自成一组：最后连它的子进程一起收掉
    procs.append(p)
    time.sleep(0.3)
    return p.pid


def emp(pid, **kw):
    return {"pid": pid, "agent": "claude", "cfg": CLAUDE_DIR, "status": "idle", "waiting": "",   # 跟 refresh() 的在班名单一样：没有 alive 字段
            "statusAt": time.time() - 3600, "term": "tmux", "pane": "%1", "socket": "/x", **kw}


try:
    old = run(os.path.join(VERSIONS, "2.1.300"), "300")
    new = run(os.path.join(VERSIONS, "2.1.317"), "300")
    check("在跑旧版：认得出（旧, 新）", crcore.outdated(emp(old)) == ("2.1.300", "2.1.317"), crcore.outdated(emp(old)))
    check("已经是新版：不用重开", crcore.outdated(emp(new)) is None)
    check("别的配置目录（LMU 之类）不管", crcore.outdated(emp(old, cfg=os.path.join(TMP, "claude-lmu"))) is None)
    check("codex：程序文件没被换掉就不算旧", crcore.outdated(emp(new, agent="codex")) is None)

    check("闲了一小时、底下没东西在跑：可以重开", crcore.free_to_restart(emp(old)))
    check("在干活：不动", not crcore.free_to_restart(emp(old, status="busy")))
    check("进程已经没了：不动", not crcore.free_to_restart(emp(999999)))
    check("弹着框等你：不动", not crcore.free_to_restart(emp(old, waiting="permission prompt")))
    check("刚停下不到 10 分钟：先不动", not crcore.free_to_restart(emp(old, statusAt=time.time() - 60)))
    check("在等后台任务：不动", not crcore.free_to_restart(emp(old, status="shell")))
    check("不在 tmux 里（没法替它打字）：不动", not crcore.free_to_restart(emp(old, term="none", pane=None)))
    busy = run("bash", "-c", "bash -c 'sleep 300; true'; true")   # 像 Claude 跑命令：员工底下挂着一个 bash
    check("底下有它开的命令还在跑：不动", not crcore.free_to_restart(emp(busy)))

    # 一轮：只重开在跑旧版、又闲着的；工号不收回；汇总一张卡
    r = crcore.Remote()
    live = {"1号": emp(old), "2号": emp(old, status="busy"), "3号": emp(new), "资讯": emp(old)}
    r.refresh = lambda: (live, live)
    closed, started, pushed = [], [], []

    def fake_close(name, s, keep_badge=False):
        closed.append((name, keep_badge))
        procs[0].kill()
        procs[0].wait()
    r.cmd_close = fake_close
    r.cmd_restart = lambda name, e: started.append((name, e["alive"])) or "claude 已起来（会话名 1号）。"
    r.push = lambda msg, kind, must=False: pushed.append((msg, kind))
    r.upgrade_tick()
    check("只重开旧版又闲着的那个（在忙的、已是新版的、资讯都不动）", [n for n, _ in closed] == ["1号"], closed)
    check("重开时工号不收回", closed == [("1号", True)])
    check("关掉后按「没在跑」去重开（不然会说它还在运行）", started == [("1号", False)], started)
    check("汇总一张卡，夜里能攒着", len(pushed) == 1 and pushed[0][1] == "upgrade" and pushed[0][1] in crcore.HOLD_KINDS
          and "2.1.300 → 2.1.317" in pushed[0][0].text and "工号不变" in pushed[0][0].text, pushed and pushed[0][0].text)
    pushed.clear()
    r.upgrade_tick()
    check("没有要重开的：不发卡", not pushed)

    # 主动下班的不报「❌ 退出了」；意外没了的照报
    r = crcore.Remote()
    sent = []
    r.push = lambda msg, kind, must=False: sent.append(kind)
    r.refresh = lambda: ({}, reg)
    reg = {"a": emp(111, alive=False, sessionId="s1"), "b": emp(222, alive=False, sessionId="s2")}   # 登记表里才有 alive
    crcore.save_json(crcore.CLOSING, {"111": time.time()})
    r.state.setdefault("mon", {})["started"] = True
    r.monitor_tick()
    check("你让它下班的不报退出，意外退出的照报", sent.count("dead") == 1, sent)
    crcore.save_json(crcore.CLOSING, {})                  # 一天后下班标记清掉了
    sent.clear()
    r.monitor_tick()
    check("下班标记过期后也不补报「退出了」", sent.count("dead") == 0, sent)
finally:
    for p in procs:
        try:
            os.killpg(p.pid, 9)
        except OSError:
            pass
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n全部通过：{len(passed)} 项")
