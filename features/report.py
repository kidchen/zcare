"""Daily admin report: what zcare did in the last 24 hours, built from the activity log."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

JOB_NAMES = {"commute": "爸爸早间路况", "outing": "妈妈出行推荐", "tv": "周五好剧", "digest": "晚间本地新闻"}
INTENT_CN = {"traffic": "路况", "weather": "天气", "outing": "出行/活动", "tv": "电视剧", "chat": "闲聊",
             "urgent": "⚠️紧急", "feedback": "反馈", "news": "新闻", "help": "帮助", "commute": "路况", "admin": "管理"}
CHANNEL_CN = {"wecom": "企业微信", "pushplus": "PushPlus"}
PURPOSE_CN = {"chat": "对话", "extract": "文章提取", "digest": "新闻小报"}
TARGET_CN = {"venue": "场馆", "category": "类别"}
REASON_CN = {"too_far": "太远", "visited": "去过了", "not_interested": "不感兴趣", "like": "喜欢"}


def _hm(ts: float, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


def build_daily_report(entries: list[dict[str, Any]], now: datetime, names: dict[str, str]) -> tuple[str, str]:
    """→ (short text for WeCom, full markdown for the detail page). ``names``: role → display name."""
    tz = now.tzinfo or ZoneInfo("Asia/Shanghai")
    by = defaultdict(list)
    for e in sorted(entries, key=lambda e: e.get("ts", 0)):
        by[e.get("type")].append(e)
    who = lambda role: names.get(role, role or "?")  # noqa: E731

    # --- scheduled messages
    sent_lines, sent_ok, sent_fallback, sent_skipped = [], 0, 0, 0
    for e in by["sent"]:
        status = "已跳过（今天没什么要说）" if e.get("skipped") else ("备用问候" if e.get("fallback") else
                                                              ("✓" if e.get("ok") else "✘ 失败"))
        if e.get("ok") and not e.get("skipped") and len(e.get("channels") or []) > 1:
            status += "（" + "+".join(CHANNEL_CN.get(c, c) for c in e["channels"]) + "）"
        sent_ok += bool(e.get("ok")) and not e.get("skipped")
        sent_fallback += bool(e.get("fallback"))
        sent_skipped += bool(e.get("skipped"))
        sent_lines.append(f"- {_hm(e['ts'], tz)} {JOB_NAMES.get(e.get('job'), e.get('job'))} → {who(e.get('to'))}：{status}")

    # --- interactions
    chats = by["chat"]
    per_user: dict[str, Counter] = defaultdict(Counter)
    for e in chats:
        per_user[e.get("role")][INTENT_CN.get(e.get("intent"), e.get("intent"))] += 1
    urgent = [e for e in chats if e.get("urgent")]
    on_demand_failed = [e for e in by["on_demand"] if not e.get("ok")]
    feedback = by["feedback"]

    # --- articles & extraction
    extracts = by["extract"]
    feed_total = max((e.get("feed_articles", 0) for e in extracts), default=0)
    new_relevant = sum(e.get("new_relevant", 0) for e in extracts)
    processed = sum(e.get("processed", 0) for e in extracts)
    failed_batches = sum(e.get("failed_batches", 0) for e in extracts)
    cached = extracts[-1].get("cached", 0) if extracts else 0
    events_ok = extracts[-1].get("events", 0) if extracts else 0
    notices_ok = extracts[-1].get("notices", 0) if extracts else 0
    feed_errors = sum(1 for e in extracts if e.get("feed_error"))

    # --- GLM
    llm: dict[str, Counter] = defaultdict(Counter)
    latency: dict[str, float] = defaultdict(float)
    for e in by["llm"]:
        p = e.get("purpose", "?")
        llm[p]["calls"] += 1
        llm[p]["ok" if e.get("ok") else "failed"] += 1
        llm[p]["prompt"] += e.get("prompt", 0)
        llm[p]["completion"] += e.get("completion", 0)
        latency[p] += e.get("latency", 0.0)

    errors = by["error"]
    cleanup = by["cleanup"]
    wemp = by["wemp_notice"]

    # ---------------------------------------------------------------- short text
    users_short = "；".join(f"{who(r)} {sum(c.values())} 次" for r, c in per_user.items()) or "无"
    order = [p for p in PURPOSE_CN if p in llm] + sorted(p for p in llm if p not in PURPOSE_CN)  # stable: chat, extract
    llm_short = "，".join(f"{PURPOSE_CN.get(p, p)} {llm[p]['calls']} 次" + (f"（失败 {llm[p]['failed']}）" if llm[p]["failed"] else "")
                         for p in order) or "无调用"
    short = [
        f"📊 zcare 日报 · {now:%m月%d日 %H:%M}（过去24小时）",
        f"定时消息：成功 {sent_ok}" + (f"，备用问候 {sent_fallback}" if sent_fallback else "")
        + (f"，跳过 {sent_skipped}" if sent_skipped else ""),
        f"家人对话：{users_short}" + (f"，⚠️紧急 {len(urgent)} 次" if urgent else ""),
        f"公众号文章：新相关 {new_relevant} 篇，已提取 {processed} 篇" + (f"，失败批次 {failed_batches}" if failed_batches else "")
        + f"；当前有效活动 {events_ok}、提醒 {notices_ok}",
        f"GLM：{llm_short}",
    ]
    if errors:
        short.append(f"❗ 异常 {len(errors)} 次：{errors[-1].get('what', '')} — {str(errors[-1].get('error', ''))[:40]}")
    if wemp:
        short.append(f"🔑 公众号授权提醒 {len(wemp)} 次（二维码{'已' if any(w.get('qr') for w in wemp) else '未'}发送）")

    # ---------------------------------------------------------------- full markdown
    md = [f"**📊 zcare 日报 · {now:%Y-%m-%d %H:%M}**", "> 过去 24 小时", ""]
    md += ["**📨 定时消息**", *(sent_lines or ["- 无"]), ""]
    md += ["**💬 家人对话**"]
    if per_user:
        for role, counts in per_user.items():
            md.append(f"- {who(role)}：{sum(counts.values())} 次（" + "、".join(f"{k}{v}" for k, v in counts.most_common()) + "）")
        md += ["", "**对话明细**"]
        md += [f"- {_hm(e['ts'], tz)} {who(e.get('role'))}「{e.get('text', '')}」→ {INTENT_CN.get(e.get('intent'), e.get('intent'))}"
               + ("（离线兜底）" if e.get("source") == "fallback" else "") for e in chats[-30:]]
    else:
        md.append("- 无")
    if urgent:
        md += ["", "**⚠️ 紧急消息**", *[f"- {_hm(e['ts'], tz)} {who(e.get('role'))}「{e.get('text', '')}」" for e in urgent]]
    if feedback:
        md += ["", "**📝 记下的偏好**", *[f"- {who(e.get('role'))}：{TARGET_CN.get(e.get('target'), e.get('target'))}"
                                      f"「{e.get('value')}」— {REASON_CN.get(e.get('reason'), e.get('reason'))}"
                                      for e in feedback]]
    if on_demand_failed:
        md += ["", "**按需请求失败**", *[f"- {_hm(e['ts'], tz)} {INTENT_CN.get(e.get('command'), e.get('command'))} → "
                                    f"{who(e.get('role'))}：{str(e.get('error', ''))[:60]}"
                                    for e in on_demand_failed]]
    md += ["", "**📰 公众号文章与提取**",
           f"- 订阅源里最近的文章：{feed_total} 篇" + (f"（拉取失败 {feed_errors} 次）" if feed_errors else ""),
           f"- 新的相关文章：{new_relevant} 篇，已送 GLM 提取：{processed} 篇"
           + (f"，失败批次：{failed_batches}" if failed_batches else ""),
           f"- 提取缓存：{cached} 篇；当前通过核验的活动 {events_ok} 个、提醒 {notices_ok} 条"]
    md += ["", "**🤖 GLM 用量**"]
    if llm:
        for p in order:
            c = llm[p]
            avg = latency[p] / c["calls"] if c["calls"] else 0
            md.append(f"- {PURPOSE_CN.get(p, p)}：{c['calls']} 次（成功 {c['ok']}，失败 {c['failed']}），"
                      f"输入 {c['prompt']} / 输出 {c['completion']} tokens，平均 {avg:.1f} 秒")
    else:
        md.append("- 无调用")
    if cleanup:
        md += ["", "**🧹 维护**", *[f"- {_hm(e['ts'], tz)} 清理旧文章：{e.get('deleted', '?')} 篇"
                                + (f"（失败：{e.get('error')}）" if e.get("error") else "") for e in cleanup]]
    if wemp:
        md += ["", "**🔑 公众号授权**", *[f"- {_hm(e['ts'], tz)} {e.get('title', '')}" + ("（已发二维码）" if e.get("qr") else "")
                                      for e in wemp]]
    if errors:
        md += ["", "**❗ 异常**", *[f"- {_hm(e['ts'], tz)} {e.get('what', '')}：{str(e.get('error', ''))[:80]}"
                                 for e in errors[-10:]]]
    return "\n".join(short), "\n".join(md)
