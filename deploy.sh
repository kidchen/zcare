#!/usr/bin/env bash
# deploy.sh — package zcare, upload to the Alibaba Cloud server, rebuild & restart the container.
#
# Usage:  ./deploy.sh [--skip-tests] [--push-env] [--package-only] [-h]
#   --skip-tests    don't run the local pytest suite first
#   --push-env      upload local .env even if the server already has one (overwrites it)
#   --package-only  build the tarball locally and list it; no SSH (for checking)
#
# Target: ZCARE_REMOTE (e.g. root@<server-ip>) from the environment or a git-ignored .deploy.env file.
# Other overrides (env vars): ZCARE_REMOTE_DIR (/root/zcare),
#                       ZCARE_SSH_PORT (22), ZCARE_SSH_KEY (path to private key, optional)
#
# Works with macOS's bash 3.2. Build happens ON the server, so the image matches its CPU arch.
set -euo pipefail

# Keep the server address out of the repo: read it from .deploy.env (git-ignored) if not exported.
if [ -z "${ZCARE_REMOTE:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/.deploy.env" ]; then
  # shellcheck disable=SC1091
  . "$(dirname "${BASH_SOURCE[0]}")/.deploy.env"
fi
REMOTE="${ZCARE_REMOTE:-}"
REMOTE_DIR="${ZCARE_REMOTE_DIR:-/root/zcare}"
SSH_PORT="${ZCARE_SSH_PORT:-22}"
SSH_KEY="${ZCARE_SSH_KEY:-}"

SKIP_TESTS=0
PUSH_ENV=0
PACKAGE_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --skip-tests)   SKIP_TESTS=1 ;;
    --push-env)     PUSH_ENV=1 ;;
    --package-only) PACKAGE_ONLY=1 ;;
    -h|--help)      sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------- output helpers
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'; else B=; G=; Y=; R=; N=; fi
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '%s✔ %s%s\n' "$G" "$*" "$N"; }
warn() { printf '%s! %s%s\n' "$Y" "$*" "$N"; }
die()  { printf '%s✘ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

[ -n "$REMOTE" ] || [ "$PACKAGE_ONLY" -eq 1 ] || {
  echo "Set the server first:  echo 'ZCARE_REMOTE=root@<server-ip>' > .deploy.env" >&2; exit 2; }
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
[ -f main.py ] && [ -f requirements.txt ] || die "Run from the zcare project (main.py/requirements.txt not found in $PROJECT_DIR)"
for cmd in tar ssh scp; do command -v "$cmd" >/dev/null || die "'$cmd' is required"; done

STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/zcare-deploy.XXXXXX")"
TARBALL="$WORK/zcare-$STAMP.tar.gz"
CONTROL_PATH="$WORK/ssh-ctl"
cleanup() {
  ssh -o ControlPath="$CONTROL_PATH" -O exit "$REMOTE" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# ---------------------------------------------------------------- 1. Docker files
write_dockerfile() {
cat > Dockerfile <<'DOCKERFILE_EOF'
# DaoCloud mirror of Docker Hub's python:3.13-slim (Docker Hub is slow/blocked in mainland China).
# Override with BASE_IMAGE in .env, e.g. python:3.13-slim outside China.
ARG BASE_IMAGE=m.daocloud.io/docker.io/library/python:3.13-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# Dependencies first so code edits don't invalidate this layer.
COPY requirements.txt .
RUN pip install --index-url "${PIP_INDEX_URL}" -r requirements.txt

# Non-root user; /app/data holds state.json and is mounted as a volume.
RUN useradd --system --uid 10001 --home-dir /app zcare \
    && mkdir -p /app/data && chown zcare:zcare /app/data

COPY --chown=zcare:zcare . .

USER zcare

EXPOSE 8000

# slim has no curl; use the stdlib.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# One process only: each extra worker would start its own scheduler (duplicate messages).
CMD ["python", "main.py", "serve", "--host", "0.0.0.0", "--port", "8000"]
DOCKERFILE_EOF
}
write_compose() {
cat > docker-compose.yml <<'COMPOSE_EOF'
name: zcare                   # fixed project name: same container/volume whatever the folder is called

services:
  zcare:
    build:
      context: .
      args:
        # Override in .env if the server can't reach Docker Hub / PyPI quickly.
        BASE_IMAGE: ${BASE_IMAGE:-m.daocloud.io/docker.io/library/python:3.13-slim}
        PIP_INDEX_URL: ${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}
    image: zcare:latest
    container_name: zcare
    env_file: .env            # all app settings; .env itself is NOT baked into the image
    environment:
      TZ: Asia/Shanghai       # log timestamps; job schedules use TIMEZONE from .env
      STATE_FILE: /app/data/state.json
    ports:
      # Behind a reverse proxy (nginx/Caddy terminating HTTPS for the WeCom callback),
      # change to "127.0.0.1:8000:8000" so the app isn't reachable directly.
      - "8000:8000"
    volumes:
      - zcare-data:/app/data  # keeps recommendation history across restarts/rebuilds
    restart: unless-stopped
    init: true                # forwards SIGTERM so the scheduler shuts down cleanly
    stop_grace_period: 20s
    # Do NOT scale this service: >1 replica = duplicate messages to your parents.
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

  # WeChat Official Account articles → JSON feeds for zcare (github.com/rachelos/we-mp-rss, MIT).
  # Logs into YOUR 公众号 backend (QR scan); talks only to Tencent. Admin UI via SSH tunnel:
  #   ssh -L 8001:127.0.0.1:8001 root@<server>  →  http://localhost:8001  (user: admin)
  we-mp-rss:
    # Nanjing University's mirror of ghcr.io (DaoCloud refuses this image: not on its allowlist).
    # Alternative: WEMP_IMAGE=docker.1ms.run/rachelos/we-mp-rss:latest (mirror the we-mp-rss README suggests)
    image: ${WEMP_IMAGE:-ghcr.nju.edu.cn/rachelos/we-mp-rss:latest}
    container_name: we-mp-rss
    environment:
      DB: sqlite:///data/db.db
      USERNAME: admin
      PASSWORD: ${WEMP_ADMIN_PASSWORD:?set WEMP_ADMIN_PASSWORD in .env}
      SECRET_KEY: ${WEMP_SECRET_KEY:?set WEMP_SECRET_KEY in .env}
      SAFE_LIC_KEY: ${WEMP_LIC_KEY:?set WEMP_LIC_KEY in .env}
      GATHER.CONTENT: "True"       # fetch full article text (default False)
      RSS_FULL_CONTEXT: "True"
      RSS_PAGE_SIZE: "100"         # caps ?limit= on the JSON feed
      MAX_PAGE: "2"                # article-list pages per account per run — gentle on WeChat's rate limit
      ARTICLE.TRUE_DELETE: "True"  # zcare's daily cleanup really removes old rows (default only marks them)
      SPAN_INTERVAL: "30"          # seconds between accounts
      REDIS_SERVER_HOST: 127.0.0.1 # its embedded Redis has no password: keep it on the container loopback
      # On login expiry, start a new login and embed its QR in the alert (zcare sends it to you as an image)
      SEND_CODE: "True"
      WERSS_SEND_CODE: "True"
      # Login-expiry alerts → zcare → your WeCom (token-protected; port 8000 is public)
      CUSTOM_WEBHOOK: http://zcare:8000/wemp/notify?token=${WEMP_NOTIFY_TOKEN:?set WEMP_NOTIFY_TOKEN in .env}
      TZ: Asia/Shanghai
    ports:
      - "127.0.0.1:8001:8001"      # its /feed/*.json has no auth — never publish this port
    # Server has 1.6 GiB RAM and no swap: cap we-mp-rss (it can launch a browser) so an out-of-memory
    # kill hits it — it restarts — rather than zcare. Raise via WEMP_MEM_LIMIT if it keeps restarting.
    mem_limit: ${WEMP_MEM_LIMIT:-800m}
    volumes:
      - wemp-data:/app/data
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  zcare-data:
  wemp-data:
COMPOSE_EOF
}
write_dockerignore() {
cat > .dockerignore <<'DOCKERIGNORE_EOF'
.env
.venv/
.git/
.claude/
**/__pycache__/
*.pyc
.pytest_cache/
data/
tests/
requirements-dev.txt
pytest.ini
zcare_plan.md
Dockerfile
docker-compose.yml
.dockerignore
deploy.sh
DOCKERIGNORE_EOF
}

step "1/5 Docker files"
[ -f Dockerfile ]         && ok "Dockerfile exists"         || { write_dockerfile;   ok "created Dockerfile"; }
[ -f docker-compose.yml ] && ok "docker-compose.yml exists" || { write_compose;      ok "created docker-compose.yml"; }
[ -f .dockerignore ]      && ok ".dockerignore exists"      || { write_dockerignore; ok "created .dockerignore"; }

# ---------------------------------------------------------------- 2. tests + package
step "2/5 Local checks & packaging"
if [ "$SKIP_TESTS" -eq 1 ]; then
  warn "skipping tests (--skip-tests)"
elif [ -x .venv/bin/python ] && .venv/bin/python -c "import pytest" 2>/dev/null; then
  .venv/bin/python -m pytest -q >"$WORK/pytest.log" 2>&1 || { tail -30 "$WORK/pytest.log"; die "tests failed — fix them or use --skip-tests"; }
  ok "tests passed ($(tail -1 "$WORK/pytest.log"))"
else
  warn "no .venv with pytest found; skipping tests"
fi

EXCLUDES=(
  --exclude=.venv --exclude=.git --exclude=.env --exclude=.claude --exclude=.idea --exclude=.vscode
  --exclude=__pycache__ --exclude='*.pyc' --exclude=.pytest_cache --exclude=.DS_Store
  --exclude=data --exclude=tests --exclude='*.log' --exclude='*.tar.gz'
  --exclude=deploy.sh --exclude=.deploy.env --exclude='.env.*' --exclude=zcare_plan.md
)
TAR_FLAGS=()
if tar --version 2>/dev/null | grep -qi bsdtar; then
  export COPYFILE_DISABLE=1                      # no macOS ._AppleDouble files
  TAR_FLAGS=(--no-mac-metadata --no-xattrs)      # avoid "unknown extended header" warnings on Linux
fi
tar ${TAR_FLAGS[@]+"${TAR_FLAGS[@]}"} "${EXCLUDES[@]}" -czf "$TARBALL" .
ok "packaged $(tar -tzf "$TARBALL" | grep -vc '/$') files ($(du -h "$TARBALL" | cut -f1 | tr -d ' '))"

if [ "$PACKAGE_ONLY" -eq 1 ]; then
  tar -tzf "$TARBALL" | grep -v '/$' | sed 's#^\./#  #'
  cp "$TARBALL" "$PROJECT_DIR/"
  ok "--package-only: wrote $(basename "$TARBALL"); nothing was uploaded"
  exit 0
fi

# ---------------------------------------------------------------- 3. SSH
SSH_OPTS=(-p "$SSH_PORT" -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new
          -o ControlMaster=auto -o ControlPath="$CONTROL_PATH" -o ControlPersist=120)
SCP_OPTS=(-P "$SSH_PORT" -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new
          -o ControlPath="$CONTROL_PATH")
if [ -n "$SSH_KEY" ]; then SSH_OPTS+=(-i "$SSH_KEY"); SCP_OPTS+=(-i "$SSH_KEY"); fi

key_setup_help() {
  cat <<HELP

  ${B}Passwordless SSH is not set up for $REMOTE.${N} One-time setup:

    ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519 -C "zcare-deploy"   # create a key if you have none
    ssh-copy-id -p $SSH_PORT $REMOTE                                      # asks for the root password once
    ssh -p $SSH_PORT $REMOTE true && echo OK                              # should not ask for a password

  Also check: Alibaba Cloud console → 安全组 (security group) allows inbound TCP $SSH_PORT from your IP.
HELP
}

step "3/5 Connecting to $REMOTE"
if ssh "${SSH_OPTS[@]}" -o BatchMode=yes "$REMOTE" true 2>"$WORK/ssh.err"; then
  ok "key-based SSH works"
else
  if grep -qiE "timed out|refused|no route|could not resolve" "$WORK/ssh.err"; then
    cat "$WORK/ssh.err" >&2
    die "cannot reach $REMOTE:$SSH_PORT — check the server is running and the security group allows SSH"
  fi
  key_setup_help
  if [ -t 0 ]; then
    printf '  Continue with password login now? (you will be asked once) [y/N] '
    read -r answer
    [ "$answer" = "y" ] || [ "$answer" = "Y" ] || die "aborted — set up the SSH key and re-run ./deploy.sh"
    ssh "${SSH_OPTS[@]}" "$REMOTE" true || die "SSH login failed"
    ok "password login works (connection reused for the rest of this run)"
  else
    die "non-interactive run needs key-based SSH"
  fi
fi

remote() { ssh "${SSH_OPTS[@]}" "$REMOTE" "$@"; }

remote 'command -v docker >/dev/null && docker compose version >/dev/null 2>&1' || die "Docker + Compose v2 not found on the server.
  Install it there, e.g. (Alibaba Cloud Linux / CentOS):
    dnf config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo
    dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin && systemctl enable --now docker
  (Ubuntu: see https://developer.aliyun.com/mirror/docker-ce)"
ok "docker compose available on server"

REMOTE_HAS_ENV="$(remote "test -f '$REMOTE_DIR/.env' && echo yes || echo no")"
UPLOAD_ENV=0
if [ "$REMOTE_HAS_ENV" = "no" ]; then
  [ -f .env ] || die "no .env on the server and none locally — create one from .env.example first"
  UPLOAD_ENV=1; warn "server has no .env — will upload your local .env"
elif [ "$PUSH_ENV" -eq 1 ]; then
  [ -f .env ] || die "--push-env given but no local .env"
  UPLOAD_ENV=1; warn "--push-env: server .env will be replaced by your local one"
else
  ok "keeping the server's existing .env (use --push-env to replace it)"
fi

# ---------------------------------------------------------------- 4. upload
step "4/5 Uploading"
REMOTE_TAR="/tmp/zcare-$STAMP.tar.gz"
REMOTE_ENV="/tmp/zcare-$STAMP.env"
scp -q "${SCP_OPTS[@]}" "$TARBALL" "$REMOTE:$REMOTE_TAR"
if [ "$UPLOAD_ENV" -eq 1 ]; then
  remote "umask 077 && : > '$REMOTE_ENV'"                 # create 0600 before secrets land in it
  scp -q "${SCP_OPTS[@]}" .env "$REMOTE:$REMOTE_ENV"
fi
ok "uploaded to $REMOTE:$REMOTE_TAR"

# ---------------------------------------------------------------- 5. remote build & start
step "5/5 Building & starting on server (first build can take a few minutes)"
remote "bash -s -- '$REMOTE_DIR' '$REMOTE_TAR' '$REMOTE_ENV' '$UPLOAD_ENV'" <<'REMOTE_SCRIPT'
set -euo pipefail
dir="$1"; tarball="$2"; envfile="$3"; upload_env="$4"
release="${dir}.new"; previous="${dir}.prev"

rm -rf "$release"; mkdir -p "$release"
tar -xzf "$tarball" -C "$release"; rm -f "$tarball"
if [ "$upload_env" = "1" ]; then
  mv "$envfile" "$release/.env"
elif [ -f "$dir/.env" ]; then
  cp -p "$dir/.env" "$release/.env"
fi
chmod 600 "$release/.env"

# Swap releases; keep one previous copy for rollback. The directory name (zcare) is also the
# compose project name, so the container and the zcare-data volume stay the same across deploys.
rm -rf "$previous"
[ -d "$dir" ] && mv "$dir" "$previous"
mv "$release" "$dir"
cd "$dir"

if ! docker compose up -d --build --remove-orphans; then
  echo "BUILD/START FAILED. If an image pull failed or timed out, set BASE_IMAGE / WEMP_IMAGE in .env to another mirror or configure a"
  echo "registry mirror in /etc/docker/daemon.json. Previous release kept at $previous"
  exit 1
fi

echo "Waiting for /health ..."
healthy=0
for _ in $(seq 1 30); do
  if docker compose exec -T zcare python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)" >/dev/null 2>&1; then
    healthy=1; break
  fi
  sleep 2
done
docker compose ps
echo "----- last log lines -----"
docker compose logs --tail 25 zcare
if [ "$healthy" != "1" ]; then
  echo "App did not become healthy within 60s (see logs above)."
  echo "Rollback: cd $previous && docker compose up -d --build   (same project name: zcare)"
  exit 1
fi
docker image prune -f >/dev/null 2>&1 || true
echo "DEPLOY_OK $(grep -E '^DRY_RUN=' .env || echo 'DRY_RUN=<unset>')"
REMOTE_SCRIPT

ok "deployed to $REMOTE:$REMOTE_DIR"
if remote "grep -qiE '^DRY_RUN=(1|true|yes|on)' '$REMOTE_DIR/.env'"; then
  warn "DRY_RUN is ON on the server — messages are logged, not sent. Set DRY_RUN=false in $REMOTE_DIR/.env, then:"
  warn "  ssh $REMOTE 'cd $REMOTE_DIR && docker compose up -d'"
fi
printf '\nUseful:\n  ssh %s "cd %s && docker compose logs -f zcare"\n  ssh %s "cd %s && docker compose exec zcare python main.py preview commute"\n' \
  "$REMOTE" "$REMOTE_DIR" "$REMOTE" "$REMOTE_DIR"
