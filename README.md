# zcare — daily WeCom notifications for Mom & Dad in Hohhot

| Job | Default schedule (Asia/Shanghai) | Recipient | What it does |
|---|---|---|---|
| `commute` | 07:30 Mon–Fri | Dad | Live driving ETA, jammed roads, weather + driving tip |
| `outing` | 09:00 daily | Mom | Picks a place (events → curated list), finds a ≤1-transfer / ≤800 m-walk bus route, gentle tips. Stays home on storms/sandstorms |
| `tv` | 19:00 Fri | Dad (configurable) | Top-rated trending 国产剧 from Douban with genre, summary, where to watch |

## Layout

```
config.py              env/.env → typed Settings (validates coords, required keys)
messages.py            Message model, markdown→text fallback, AMap deep links
storage.py             JSON state (avoids repeating places/dramas)
clients/
  http.py              requests.Session with GET-only retries
  amap_client.py       driving (traffic TMC), transit, weather, POI search
  wecom_client.py      access_token cache/refresh, text / markdown / textcard
features/
  commute.py  outing.py  tv_recommend.py
main.py                FastAPI + APScheduler + CLI
tests/                 offline tests with fake HTTP sessions
```

## Setup

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # fill in AMAP_KEY, coordinates; keep DRY_RUN=true at first
pytest -q
```

## Run

```bash
python main.py preview commute     # build + print, send nothing (no state recorded)
python main.py preview outing
python main.py preview tv
python main.py send commute        # really send one now
python main.py serve               # API + scheduler (or: uvicorn main:app, single worker)
```

HTTP:
- `GET /health` — next run time per job
- `POST /jobs/{commute|outing|tv}/run?dry_run=true` — returns the message. Real sends
  (`dry_run=false`) require `ADMIN_TOKEN` set and header `X-Admin-Token`.
- `GET /wecom/webhook` — WeCom 接收消息 URL verification. Set `WECOM_CALLBACK_TOKEN` +
  `WECOM_ENCODING_AES_KEY` (same values as the console) and `WECOM_CORP_ID`, deploy, then click
  保存 in 应用 → 接收消息 → 设置API接收. The URL must be reachable from the public internet, and
  WeCom generally requires the domain to be ICP-filed (备案) and tied to your company.
- `POST /wecom/webhook` — messages parents send to the app. Keyword commands (see `bot.py`):
  `路况`/`堵车` → commute, `剧`/`电视` → dramas, `出门`/`去哪`/`逛`/`公交` → outing, anything else → help.
  Replies instantly with an encrypted "稍等" ack, then pushes the result via the send API
  (WeCom only waits ~5 s). Only father/mother/admin user IDs are served; retries are de-duplicated
  by MsgId; 60 s per-user cooldown per command; on-demand runs don't affect scheduled rotation.

## LLM family assistant (智谱 BigModel / DeepSeek)

Set `LLM_API_KEY` (default `LLM_PROVIDER=bigmodel`: bigmodel.cn → API Keys, model `glm-4-flash-250414` with `glm-4.7-flash` as fallback;
or `LLM_PROVIDER=deepseek`) and every text a
parent sends goes to the assistant instead of the keyword bot. It classifies intent
(`traffic` / `outing` / `tv` / `chat` / `urgent`) and writes a warm reply; real data still comes from
the feature modules (the prompt forbids inventing it). `urgent` (chest pain, falls, scam signs) also
alerts `ADMIN_WECOM_USERID`; a keyword safety net does this even if the LLM misclassifies or is down,
and on any LLM failure the bot falls back to keyword commands.

```bash
python main.py chat "今天有点胸闷" --as mother     # try it locally; nothing is sent
```

Privacy: parents' messages (and their display names) are sent to the LLM provider. Prompt: `features/assistant.py`.

## Content for 60+ parents

- **Dramas** (`features/tv_recommend.py`): each recommendation = `TV_NEW_PICKS` (2) current hits from
  Douban 热门 国产剧 (aired this year or last) + classics from Douban 选影视 genre lists (`TV_CLASSIC_GENRES`,
  3 random genres per run). Quality gate: rating ≥ `TV_MIN_RATING` (7.5), or for new shows ≥ 7.0 with
  ≥ 20 000 votes; unrated shows are skipped. Only clearly unsuitable genres are excluded
  (`TV_EXCLUDED_GENRES`: animation, horror, reality/talk shows, kids'). Picks are spread across genres,
  new ones are marked 🔥新剧, iQiyi availability ranks higher and each card says how to find it on
  **奇异果TV** (remote-search initials, 大明王朝1566 → `DMWC`). Never repeats recent picks.
- **Local events & reminders — no web search** (`features/events.py`). Sources:
  - **WeChat Official Account articles** via self-hosted **we-mp-rss** (second container in
    docker-compose; open source, MIT, talks only to Tencent servers). The free GLM model only *extracts*: events (opera, 二人台, concerts, exhibitions,
    cultural-centre activities, square/park folk events) from the last 3 weeks, and traffic / utility
    reminders (closures, heating, water/power cuts) from the last 3 days. Every item is kept only if its
    date, venue, price, quote and numbers are literally in the article, and AMap confirms the venue.
  - **Official weather warnings** from the National Meteorological Center (nmc.cn), filtered to Hohhot,
    used verbatim (no LLM). Cancelled (解除) and > 48 h old warnings are dropped.
  Reminders appear as "📢 今日提醒" in Dad's commute (weather, utilities, traffic) and Mom's outing (weather,
  utilities), and as `Notes=` in the chat context (cache only). Falls back to `CURATED_PLACES`.

### we-mp-rss one-time setup

1. Register a free personal **WeChat Official Account (订阅号)** at mp.weixin.qq.com (real-name
   verification). It never needs to publish anything; it is only used to log into the backend, from which
   we-mp-rss lists other accounts' articles.
2. In `.env`: `WEMP_RSS_URL=http://we-mp-rss:8001`, plus random values for `WEMP_ADMIN_PASSWORD`,
   `WEMP_SECRET_KEY`, `WEMP_LIC_KEY`, `WEMP_NOTIFY_TOKEN` (`python3 -c "import secrets;print(secrets.token_urlsafe(18))"`),
   and make sure `ADMIN_WECOM_USERID` is set (login-expiry alerts go there). Deploy: `./deploy.sh --push-env`.
3. Tunnel to the admin UI (bound to the server's localhost only — its feed API has no auth):
   `ssh -L 8001:127.0.0.1:8001 root@<server-ip>`, open http://localhost:8001, log in as `admin`.
4. Scan the authorization QR code with the WeChat account that administers your 订阅号.
5. Add subscriptions (search by account name) — keep it to ~10–15 local accounts, added a few at a time:
   local news (e.g. 新闻天天看, 青橙要闻), traffic police, 乌兰恰特大剧院, museums, the cultural centre.
   Set the update schedule in its UI to a few times a day (WeChat rate-limits article-list requests).
6. **Re-authorization from your phone (no SSH):** the backend login expires every few days. we-mp-rss then
   starts a new login and calls `/wemp/notify` with the QR (`SEND_CODE=True`); zcare sends it to
   `ADMIN_WECOM_USERID` as a WeCom **image**. Scan it with the WeChat that administers your 订阅号 (long-press
   → 识别图中二维码, or open it on another screen and scan). QR codes expire after a few minutes — reply
   **「扫码」** to the bot (admin only, max once a minute) and zcare asks we-mp-rss for a fresh one via its API
   (`WEMP_ADMIN_PASSWORD`). Until you re-scan, zcare uses official warnings + curated places.
7. Optional: restrict zcare to specific accounts with `WEMP_RSS_FEEDS=<id>,<id>` (ids in its UI).
8. **Retention:** every day at 03:30 zcare calls we-mp-rss `DELETE /api/v1/wx/articles/clean-old?days=N`
   (`WEMP_RETENTION_DAYS`, default 7; `ARTICLE.TRUE_DELETE=True` makes it a real delete). zcare's event
   look-back follows the same number — use 14 if events announced 1–2 weeks ahead get missed.

- AMap place lookups are name-checked (`poi_name_matches`): AMap's fuzzy top hit is often a different
  place (呼和浩特市文化馆 → 将军衙署博物院), which is now rejected instead of routed to.

## Evening local-news digest

Every day at `DIGEST_CRON` (default 19:30, after Friday's 19:00 drama message) both parents get up to
`DIGEST_MAX_ITEMS` (default 5) local news items from the subscribed accounts — no topic/source filter, just
ranking. It's one WeCom `news` message (a single bubble, like a 公众号 multi-article push) with one row per item;
tapping a row opens the **original** WeChat article directly. Covers come from the we-mp-rss RSS feed
(`<enclosure>`; its JSON feed has none) and are served from zcare's `/posters/` (WeChat's image CDN shows a
"not allowed to quote" placeholder when loaded with a foreign Referer). Links from we-mp-rss's WeRead mode
that contain `~` (pre-2026-09-23 bug → WeChat 「参数错误」) are repaired to `_`.

1. Candidates: today's/yesterday's articles not already sent. The same story from several accounts is grouped by
   title similarity (titles with different numbers are kept apart), and stories already told are skipped too.
2. GLM ranks them (titles + opening lines), then writes a plain title + 1–2 sentence summary for the top few.
3. Every number in the GLM title/summary must appear in the article; otherwise — or if GLM is down — the card
   uses the article's own title and first sentence. No articles → nothing is sent.

Asking 「新闻」/「有啥新鲜事」 sends the current digest on demand (doesn't mark items as sent). Preview:
`python main.py preview digest`; turn off with `DIGEST_CRON=`.

## Daily admin report

Every day at `DAILY_REPORT_CRON` (default 21:00) `ADMIN_WECOM_USERID` gets a short WeCom text — scheduled
messages sent / fallbacks / skips, each parent's chats by intent (⚠️ urgent highlighted), new articles and
extraction results, GLM calls and failures, auth alerts, errors — plus a link to the full report page
(chat log with short excerpts, recorded preferences, token totals and latency, cleanup). Source:
`data/activity.jsonl` (append-only, survives restarts, kept 8 days). Preview: `python main.py report`.

## GLM usage

All calls use the free `glm-4-flash-250414` (fallback `glm-4.7-flash`), labelled by purpose:
- **chat** — one call per parent message (~700 prompt tokens, 1–2 s, temperature 0.5, 20 s timeout).
- **extract** — each new relevant WeChat article is sent **once**, in batches of 6 (measured: ~2.3k prompt
  tokens, ~12 s per batch), with a date-independent prompt; raw results live in `data/article_extracts.json`
  and are re-checked against today on every use (~3 ms). A warm-up job at 06:45/11:45/17:45 (after the
  we-mp-rss fetches) extracts ahead of time. Extraction gets a 120 s timeout and 2 attempts; replies cut off
  at `max_tokens` fail fast. Before: one 40-article prompt (~7.4k tokens, ~30 s) resent on every refresh,
  which also exceeded the old 20 s timeout.
- Daily counters per purpose (calls / ok / failed / tokens / avg latency) are in `/health` (admin token) and
  each call logs `LLM usage [purpose] model: prompt=… completion=…`.

## Learning, calendar, never-fail

- **Preferences from chat** (`features/preferences.py`): "这个太远了", "美术馆那个去过了", "我们不喜欢看展览",
  "上次的演出挺好的" → intent `feedback`. GLM only decides *that* it's feedback and *why*; the target is resolved
  in code from the parent's words and what was recommended in the last 3 days (unmatched feedback is dropped).
  Effects: visited → venue skipped 30 days; too far → venue skipped 180 days, and after 2 such complaints only
  routes ≤ 45 min; not interested → venue/category pushed down 90 days (a category said twice is excluded);
  like → category ranked first. Known tastes are passed to the chat as `Prefs=`.
- **Workdays / weekends / holidays** (`features/daytype.py`, official calendar incl. 调休 via `chinesecalendar`):
  Dad's 07:30 job runs daily — full commute on workdays (incl. make-up Saturdays); on days off no traffic, only a
  short greeting when there's a weather/utility notice, otherwise nothing. Fridays and days off bias Mom's events
  toward ones happening on days off and leisure categories, with a weekend/holiday heading. Update
  `chinesecalendar` each November when the next year's schedule is published (until then: Mon–Fri rule).
- **Never fail**: feed down / no verified events → curated places + official NMC warnings; AMap weather down →
  message without weather; AMap down entirely → a classic place with an AMap search button; commute route down →
  greeting + weather + button; any unexpected error → a warm fallback greeting (and an alert to you).

## Detail pages behind cards

With `WECOM_MESSAGE_TYPE=news`/`textcard`, cards that would open AMap (commute, outing, events) open
`PUBLIC_BASE_URL/detail/<random id>` instead: the full, untruncated message in large type on a cream
background, with a big **📍 打开高德地图导航** button (one per event) at the bottom. Card descriptions are short
summaries ending in "点开看… ›". Drama cards keep their Douban links. Pages are stored under the data volume,
use unguessable 128-bit ids (they contain the home location via the AMap link), expire after 14 days, and are
served with no-store / no-referrer / noindex headers.

## Gotchas

- **Markdown doesn't render in regular WeChat.** WeCom `markdown` only shows in the WeCom app.
  If your parents receive messages via WeChat (微信插件), set `WECOM_MESSAGE_TYPE=text`
  (or `textcard`, which shows a tappable card linking to AMap).
- **Trusted IP.** New WeCom self-built apps must list the server's public IP under
  应用 → 企业可信IP, otherwise sends fail with errcode `60020`.
- **One process only.** Each uvicorn worker would start its own scheduler → duplicate messages.
  (WeCom duplicate-check is enabled as a backstop, 10-minute window.)
- **APScheduler day numbers:** `0` = Monday, not Sunday. Use names (`mon-fri`, `fri`).
- **Coordinates are GCJ-02 `lng,lat`.** Use the [AMap picker](https://lbs.amap.com/tools/picker);
  config rejects values that look swapped.
- **调休 workdays** (weekend make-up days) aren't handled — the commute job follows `COMMUTE_CRON`.
- **AMap key** must be a "Web服务" key. Weather API gives conditions but no official warnings.
- **Douban** may return 403/418 if hit too often; detail fetches are throttled (1.5 s) and capped at 8/run.
  Set `DOUBAN_COOKIE` (`bid=...`) if blocked.
- **Curated places** in `features/outing.py` are a seed list — check names/closing days with Mom.
  For events, point `OUTING_EVENT_URL` + `OUTING_EVENT_ITEM_SELECTOR` at a venue's exhibitions list
  page (selector from browser devtools) and set `OUTING_EVENT_VENUE`.

## Deploy (one command)

```bash
./deploy.sh                # tests → package → scp → docker compose up -d --build → health check
./deploy.sh --push-env     # also replace the server's .env with your local one
./deploy.sh --package-only # just build/list the tarball, no SSH
./deploy.sh --fetch-backup # download the newest nightly data backup into ./backups/ (no deploy)
```

Target defaults to `root@<server-ip>:/root/zcare` (override with `ZCARE_REMOTE`, `ZCARE_REMOTE_DIR`,
`ZCARE_SSH_PORT`, `ZCARE_SSH_KEY`). One-time passwordless SSH setup:

```bash
ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519 -C zcare-deploy
ssh-copy-id root@<server-ip>
```

Each deploy unpacks into a fresh directory (the previous one is kept as `/root/zcare.prev` for
rollback), keeps the server's `.env` unless `--push-env`, and fails loudly if `/health` doesn't come up.

## Normal WeChat via PushPlus

WeCom's 微信插件 isn't available for this account, so scheduled messages can also go to normal WeChat
through [PushPlus](https://www.pushplus.plus) (its 服务号 "pushplus 推送加"; pin it in WeChat so it stays on top).

- `DELIVERY_CHANNELS=wecom,pushplus` sends every scheduled message to both; a failing channel is reported
  under ❗ 异常 in the daily report but doesn't stop the other. Chat and on-demand replies stay in WeCom.
- The notification shows only ~12 characters of the title (emoji dropped), so each message has a
  `headline` with the key fact first: 「局部缓行 车程22分钟」, 「今晚5条本地新闻：…」, 「好剧3部：《…》」.
- Tapping opens the full message as a large-font page with the AMap / 阅读原文 buttons. Images must be
  `https`: 公众号 covers load straight from WeChat's CDN (no Referer); images from this server (`http`)
  are blocked by the https page, so drama posters aren't shown there.
- Recipients: Dad/Mom scan your PushPlus QR, then put their 好友令牌 in `FATHER_PUSHPLUS_TO` /
  `MOTHER_PUSHPLUS_TO`. Empty = you (the token owner) — handy for testing.
- Free tier (real-name verified): 200 requests/day, 5/minute — zcare uses ~5/day. `code 200` only means
  PushPlus accepted the message. Content passes through PushPlus's servers (history kept 30 days).

## Data safety & monitoring

- **State files can't silently reset.** `data/state.json` (sent dramas/news, preferences) and
  `data/article_extracts.json` are written atomically, and each write first keeps the previous good copy
  as `<name>.bak`. A file that can't be read is moved aside to `<name>.corrupt-<time>` (never overwritten)
  and the `.bak` copy is restored; the problem shows up under ❗ 异常 in the 21:00 report.
- **Nightly backup (03:45):** `state.json`, `article_extracts.json` and `activity.jsonl` are copied to
  `data/backups/<YYYYMMDD>/` in the data volume; the newest 7 days are kept. To keep a copy off the server:
  `./deploy.sh --fetch-backup` → `backups/zcare-data-<time>.tar.gz` (git-ignored). Restore: stop zcare,
  copy the files back into the volume (`docker compose cp <file> zcare:/app/data/`), start it.
- **Watchdog:** `deploy.sh` installs a systemd timer on the server (`zcare-watchdog.timer`, every 5 min).
  If the zcare container is down/unhealthy or we-mp-rss isn't running for 2 checks in a row,
  `ADMIN_WECOM_USERID` gets one WeCom text; another when it recovers. It reads the WeCom settings from the
  server's `.env`. Check it: `systemctl list-timers zcare-watchdog.timer`, `journalctl -u zcare-watchdog`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
scripts/install-hooks.sh          # once per clone: pre-commit = privacy check + ruff
.venv/bin/python -m pytest -q
.venv/bin/ruff check .            # --fix for import order etc.
```

- **Privacy check** (`scripts/privacy_check.py`, also `--all` for every tracked file): blocks a commit that
  contains any 8+-character `.env` value not also in `.env.example` (keys, secrets, home/work coordinates
  and addresses, user ids), or the server address from `.deploy.env`. Add your own regexes, one per line,
  to the git-ignored `.privacy-patterns` (e.g. family names, your neighbourhood, phone numbers).
- **CI** (`.github/workflows/ci.yml`): ruff + pytest on every push and pull request. `deploy.sh` runs the
  same checks locally before uploading.
- GLM prompts in `features/assistant.py`, `events.py` and `digest.py` keep one rule per line (exempt from
  the line-length rule): re-wrapping them would change what the model sees.

## Docker

```bash
cp .env.example .env && vim .env          # on the server; never baked into the image
docker compose up -d --build
docker compose logs -f zcare              # look for "scheduled commute next run ..."
curl localhost:8000/health
docker compose exec zcare python main.py preview commute
```

- State (`state.json`) lives in the named volume `zcare-data`; it survives rebuilds.
- Port 8000 is published on all interfaces. For the WeCom callback put nginx/Caddy (HTTPS, your
  备案 domain) in front and change the mapping to `127.0.0.1:8000:8000`; also open only 80/443 in the
  Alibaba Cloud firewall.
- Never `--scale zcare=2` — each replica runs its own scheduler.
- Changes to `.env` need `docker compose up -d` (recreate), not just `restart`.
