#!/usr/bin/env bash
# 示例：自己的安全启动脚本（config.env 里 CLAUDE_MONITOR_LAUNCH_SCRIPT 指向它）。
# 机器人新开 / 叫回员工时调用：claude-launch.sh <会话名> <工作目录> [claude 参数...]
# 在 $CLAUDE_MONITOR_LAUNCH_SOCK 这个 tmux（你平常用的那个）里开一个叫 <会话名> 的新会话，机器人再把它挪到员工该在的窗口。
# 任何一项检查不过就不启动（退出码非 0，原因写在 stderr）。下面是在共用服务器上的例子：
#   隔离的 HOME / CLAUDE_CONFIG_DIR、必须经本机代理上网、工作目录只能在自己的目录下。
set -euo pipefail
name=${1:?需要会话名}; dir=${2:?需要工作目录}; shift 2
MY_ROOT=$HOME                       # 改成你自己的目录
PROXY=127.0.0.1:10000               # 改成你的代理；不需要代理就删掉代理那两段
private=$(cd "$(dirname "$0")/.." && pwd)/tmux.sock
sock=${CLAUDE_MONITOR_LAUNCH_SOCK:-$private}

fail() { echo "启动中止：$*" >&2; exit 1; }
[ -d "$dir" ] || fail "工作目录不存在：$dir"
case "$(realpath "$dir")" in "$MY_ROOT"|"$MY_ROOT"/*) ;; *) fail "工作目录不在 $MY_ROOT 下：$dir" ;; esac
command -v claude >/dev/null || fail "找不到 claude"

export HTTPS_PROXY=http://$PROXY HTTP_PROXY=http://$PROXY ALL_PROXY=http://$PROXY
export https_proxy=$HTTPS_PROXY http_proxy=$HTTP_PROXY all_proxy=$ALL_PROXY
(exec 3<>/dev/tcp/${PROXY%:*}/${PROXY#*:}) 2>/dev/null || fail "代理 $PROXY 没在监听"

if tmux -S "$sock" has-session -t "=$name" 2>/dev/null; then
  [ "$sock" = "$private" ] || fail "tmux 会话 $name 已经有了，不动它"
  tmux -S "$sock" list-panes -s -t "=$name" -F '#{pane_current_command}' | grep -qx claude && fail "会话 $name 里的 claude 还在运行"
  tmux -S "$sock" kill-session -t "=$name"
fi
# tmux 已经开着时，新窗格拿的是 tmux 自己的环境：代理用 -e 带过去
tmux -S "$sock" new-session -d -s "$name" -c "$dir" -x 220 -y 50 \
  -e HTTPS_PROXY=$HTTPS_PROXY -e HTTP_PROXY=$HTTP_PROXY -e ALL_PROXY=$ALL_PROXY \
  -e https_proxy=$https_proxy -e http_proxy=$http_proxy -e all_proxy=$all_proxy \
  claude "$@"
echo "已在 tmux 会话 $name 启动：claude $*"
