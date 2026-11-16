#!/usr/bin/env bash
# =============================================================================
# 本地发布脚本：把已提交的功能分支合并进 main，双推送并做健康检查
#
# 用法（在仓库根目录）：
#     bash deploy/ship.sh feat/<name>          # 交互确认
#     bash deploy/ship.sh feat/<name> --yes    # 已获人工批准，跳过确认
#
# 前提：
#   · 功能分支的改动已经提交；工作区没有未提交的已跟踪文件。
#   · 用户已经审阅并认可这次改动。
#   · server remote 已配置（见 CLAUDE.md「Git 工作流与云服务器部署」）。
# =============================================================================
set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin:$PATH

cd "$(git rev-parse --show-toplevel)"

BRANCH="${1:-}"
CONFIRM="${2:-}"
if [ -z "$BRANCH" ] || [ "$BRANCH" = "main" ]; then
  echo "用法: bash deploy/ship.sh <feature-branch> [--yes]"
  echo "  <feature-branch> 不能是 main"
  exit 2
fi

if ! git rev-parse --verify --quiet "$BRANCH" >/dev/null; then
  echo "❌ 分支不存在: $BRANCH"
  exit 2
fi

# ---- 1. 工作区必须干净（忽略未跟踪/被忽略的本地数据）----
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "❌ 有未提交的已跟踪改动，请先提交："
  git status --short --untracked-files=no
  exit 1
fi

# ---- 2. 私钥安全边界 ----
if git ls-files --error-unmatch id_ed25519 >/dev/null 2>&1; then
  echo "❌ id_ed25519 已被 git 跟踪，拒绝发布"
  exit 1
fi
if ! git check-ignore -q id_ed25519; then
  echo "❌ id_ed25519 未被 .gitignore 忽略，拒绝发布"
  exit 1
fi
if grep -qxF 'id_ed25519' .dockerignore 2>/dev/null; then
  :
else
  echo "❌ .dockerignore 未排除 id_ed25519，拒绝发布"
  exit 1
fi
# 只匹配真正的 PEM 头（带五个连字符）；排除检查脚本自身，避免自匹配
if git grep -I -l -E '^-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----' -- . \
     ':(exclude)deploy/ship.sh' ':(exclude)deploy/post-receive' >/dev/null 2>&1; then
  echo "❌ 已跟踪文件中发现私钥内容，拒绝发布"
  exit 1
fi

# ---- 3. 远程与确认 ----
git remote get-url origin >/dev/null 2>&1 || { echo "❌ 缺少 origin remote"; exit 1; }
git remote get-url server >/dev/null 2>&1 || { echo "❌ 缺少 server remote"; exit 1; }

if [ "$CONFIRM" != "--yes" ]; then
  printf '已获得人工批准，合并 %s 到 main 并双推送？[y/N] ' "$BRANCH"
  read -r ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "已取消"; exit 1 ;;
  esac
fi

# ---- 4. 合并到最新 main ----
git fetch origin --prune
git switch main
git merge --ff-only origin/main
git merge --no-ff "$BRANCH" -m "merge: $BRANCH"

# ---- 5. 双推送：GitHub 备份 + 服务器部署 ----
git push origin main
if ! git push server main; then
  echo "❌ 服务器推送/部署失败；查看日志："
  echo "   ssh -i F:/llm-wiki/wiki/wiki/id_ed25519 root@8.155.135.176 'tail -100 /var/log/pharma-deploy.log'"
  exit 1
fi

# ---- 6. 公网健康检查（最多约 2 分钟）----
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 http://8.155.135.176:8765/api/health >/dev/null 2>&1; then
    echo "✅ 发布完成，线上健康检查通过"
    exit 0
  fi
  sleep 2
done

echo "❌ 线上健康检查失败"
echo "   查看日志：ssh -i F:/llm-wiki/wiki/wiki/id_ed25519 root@8.155.135.176 'tail -100 /var/log/pharma-deploy.log'"
exit 1
