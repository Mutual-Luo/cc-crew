#!/usr/bin/env bash
# 由 cc-crew 在新 tmux 窗口里调用：加载这套 codex 自己的环境（比如 codex-env.sh / codex2-env.sh）后，
# 在工作目录里跑交互式 codex；codex 退出后留下一个环境一样的 shell，方便手动查看或再启动。
#   codex-inner.sh [--proxy URL] [--home CODEX_HOME] <环境脚本|-> <工作目录> [codex 参数...]
proxy= home=
while [ $# -gt 0 ]; do
  case "$1" in
    --proxy) proxy=$2; shift 2 ;;
    --home) home=$2; shift 2 ;;
    *) break ;;
  esac
done
envf=$1 dir=$2
shift 2
# 私有 tmux 服务器是 ssh 登录里的 Claude Code 起的，会把 ssh / VS Code / Claude Code 的变量带给每个窗格：codex 用不着，清掉
for v in $(compgen -e); do
  case $v in SSH_CLIENT|SSH_CONNECTION|SSH_TTY|VSCODE_*|CLAUDECODE|CLAUDE_CODE_*|CLAUDE_PID) unset "$v" ;; esac
done
[ "$envf" != - ] && source "$envf"
[ -n "$home" ] && export CODEX_HOME="$home"
if [ -n "$proxy" ]; then
  export https_proxy=$proxy http_proxy=$proxy all_proxy=$proxy HTTPS_PROXY=$proxy HTTP_PROXY=$proxy ALL_PROXY=$proxy
fi
cd "$dir" && codex "$@"
echo "[codex 已退出，退出码 $?]。这个 shell 已加载 ${envf}（CODEX_HOME=$CODEX_HOME）。"
exec bash --norc -i
