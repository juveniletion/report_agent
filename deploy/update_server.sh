#!/usr/bin/env bash
#
# update_server.sh — 从 GitHub 更新代码并重建容器
#
# 用法（在云服务器上）：
#     cd /opt/pharma-cost-wiki
#     bash update.sh
#
# 不会被触碰的东西：
#   · .env
#   · raw/ wiki/ state/ graph/ reports/ exports/
#   · Docker 命名卷 pharma-cost-wiki-memory（auth.db / users/*.db / knowledge.db）
#
# ⚠️ 本脚本以 GitHub main 为准，会覆盖服务器上的本地代码改动。
#    首次运行时，当前服务器版本会被保存到本地分支 server-baseline；
#    之后每次运行前的版本保存在 server-prev，可用它回滚。
#
# ⚠️ 当前服务器上的「无 LLM 兜底」改动（server/app.py、report_build.py、
#    graph_build_raw.py）没有提交到 GitHub。第一次运行本脚本后，这些改动
#    只存在于 server-baseline 分支，不会再出现在工作区。若要长期保留，
#    请先把它们提交并 push 到 GitHub。

set -euo pipefail

APP_DIR="/opt/pharma-cost-wiki"
REPO="https://github.com/juveniletion/report_agent.git"
BRANCH="main"

cd "$APP_DIR"

if [ ! -f .env ]; then
  echo "错误：缺少 $APP_DIR/.env，请先创建配置再更新。" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 1. 首次：把当前服务器代码保存为 server-baseline，再切到 origin/main
# ---------------------------------------------------------------------------
if [ ! -d .git ]; then
  echo "[init] 初始化 git，并把当前服务器版本保存为 server-baseline"
  git init -q
  git config user.name "server"
  git config user.email "server@local"
  # 这两个文件不在 GitHub 仓库里，但必须留在服务器上：脚本自身 + 给 Claude 的说明
  printf '%s\n' "update.sh" "CLAUDE.md" >> .git/info/exclude
  git add -A
  git commit -qm "server baseline $(date +%F-%H%M%S)" || true
  git branch -f server-baseline HEAD
  git branch -f server-prev HEAD
  git remote add origin "$REPO" 2>/dev/null || git remote set-url origin "$REPO"
  git fetch origin "$BRANCH"
  git checkout -B "$BRANCH" "origin/$BRANCH"
else
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "$REPO"
  git fetch origin "$BRANCH"
  git branch -f server-prev HEAD 2>/dev/null || true
  git reset --hard "origin/$BRANCH"
fi

# ---------------------------------------------------------------------------
# 2. 重新应用国内镜像源（git reset 会覆盖 Dockerfile）
# ---------------------------------------------------------------------------
if ! grep -q "mirrors.aliyun.com/pypi" Dockerfile; then
  sed -i "s|RUN pip install --no-cache-dir -r requirements.txt|RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt|" Dockerfile
fi
if ! grep -q "mirrors.aliyun.com/pypi" server/Dockerfile.mock; then
  sed -i "s|RUN pip install --no-cache-dir -r requirements-mock.txt|RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements-mock.txt|" server/Dockerfile.mock
fi
python3 - <<'PY'
from pathlib import Path
p = Path("Dockerfile")
s = p.read_text()
needle = "RUN apt-get update && apt-get install -y --no-install-recommends"
repl = 'RUN sed -i "s/deb.debian.org/mirrors.aliyun.com/g" /etc/apt/sources.list.d/debian.sources && apt-get update && apt-get install -y --no-install-recommends'
if needle in s:
    p.write_text(s.replace(needle, repl))
PY

# ---------------------------------------------------------------------------
# 3. 重建并启动（不会执行 down -v）
# ---------------------------------------------------------------------------
docker compose up -d --build
docker compose ps
echo
curl -fsS http://127.0.0.1:8765/api/health || true
echo
echo "✅ 更新完成。.env、数据目录和数据库卷均未受影响。"
echo "   回滚代码：git reset --hard server-prev && docker compose up -d --build"
