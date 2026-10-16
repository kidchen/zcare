#!/usr/bin/env bash
# zcare-watchdog — runs ON the server (systemd timer, every 5 min). Messages the admin on WeCom when the
# zcare or we-mp-rss container is down/unhealthy, because a dead zcare can't report itself (and the 21:00
# report wouldn't arrive either). Alerts once after 2 consecutive bad checks (deploy restarts don't page)
# and once on recovery. Installed by deploy.sh.   Usage: zcare-watchdog /root/zcare/.env
set -u
ENV_FILE="${1:?usage: zcare-watchdog <path to zcare .env>}"
STATE_DIR="${ZCARE_WATCHDOG_STATE:-/var/lib/zcare-watchdog}"
mkdir -p "$STATE_DIR"

state() { docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null || echo "missing/none"; }
zcare=$(state zcare); wemp=$(state we-mp-rss)
case "$zcare" in running/starting) exit 0 ;; esac          # just (re)started: decide next run
problems=""
[ "$zcare" = "running/healthy" ] || problems="zcare（${zcare}）"
case "$wemp" in running/*) ;; *) problems="${problems:+$problems、}we-mp-rss（${wemp}）" ;; esac

env_value() {  # VALUE of KEY=VALUE in .env, surrounding quotes removed
  grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

send() {
  local corp secret agent admin token body
  corp=$(env_value WECOM_CORP_ID); secret=$(env_value WECOM_SECRET)
  agent=$(env_value WECOM_AGENT_ID); admin=$(env_value ADMIN_WECOM_USERID)
  [ -n "$corp" ] && [ -n "$secret" ] && [ -n "$agent" ] && [ -n "$admin" ] || { echo "watchdog: WeCom settings missing in $ENV_FILE"; return 1; }
  # Secrets go to curl via stdin (-K -), never on the command line / process list.
  token=$(printf 'url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=%s&corpsecret=%s"\n' "$corp" "$secret" |
          curl -sS -m 20 -K - | sed -n 's/.*"access_token" *: *"\([^"]*\)".*/\1/p')
  [ -n "$token" ] || { echo "watchdog: could not get a WeCom access token"; return 1; }
  body=$(printf '{"touser":"%s","msgtype":"text","agentid":%s,"text":{"content":"%s"}}' "$admin" "$agent" "$1")
  printf 'url = "https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=%s"\n' "$token" |
    curl -sS -m 20 -K - -H 'Content-Type: application/json' --data-binary "$body" | grep -q '"errcode" *: *0'
}

count=$(cat "$STATE_DIR/bad_count" 2>/dev/null || echo 0)
alerted=$([ -f "$STATE_DIR/alerted" ] && echo 1 || echo 0)
if [ -n "$problems" ]; then
  count=$((count + 1)); echo "$count" > "$STATE_DIR/bad_count"
  if [ "$count" -ge 2 ] && [ "$alerted" = 0 ]; then
    send "🚨 zcare 服务器异常：${problems} 未正常运行，爸妈的消息可能发不出去。查看：cd $(dirname "$ENV_FILE") && docker compose ps && docker compose logs --tail 50" \
      && touch "$STATE_DIR/alerted"
  fi
else
  echo 0 > "$STATE_DIR/bad_count"
  if [ "$alerted" = 1 ]; then
    send "✅ zcare 已恢复正常（zcare: ${zcare}，we-mp-rss: ${wemp}）" && rm -f "$STATE_DIR/alerted"
  fi
fi
