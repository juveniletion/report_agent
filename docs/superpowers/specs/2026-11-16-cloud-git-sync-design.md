# 云服务器 Git 同步与发布流程设计

- 日期：2026-11-16
- 状态：已确认（2026-11-16，用户批准执行；实现分支 `feat/server-sync-setup`）
- 服务器：`root@8.155.135.176:22`

## 1. 目标

1. 在云服务器建立 Git 部署源，使本地 `main` 可以一键同步并自动重建容器。
2. 固定「先建功能分支 → 用户认可 → 提交 → 合并 main → 双推送」的流程。
3. 保证本地私钥 `id_ed25519` 永不进入 Git、Docker 构建上下文或云服务器。
4. 部署过程不触碰服务器上的 `.env`、业务数据和数据库卷。

## 2. 非目标

- 不改变应用代码的运行逻辑。
- 不把 `raw/`、`wiki/`、`state/`、`graph/`、`reports/`、`exports/`、`data/` 纳入版本库。
- 不引入 CI 服务；部署由服务器端 `post-receive` 钩子完成。
- 不自动回滚；失败时保留回滚点并打印命令，由人决定。

## 3. 拓扑

```
本地 F:\llm-wiki\wiki\wiki
├── origin  → GitHub（异地备份）
└── server  → root@8.155.135.176:/opt/pharma-cost-wiki.git（部署源）

服务器
├── /opt/pharma-cost-wiki.git        bare 仓库
│   └── hooks/post-receive            只对 main 更新执行部署
├── /opt/pharma-cost-wiki            工作区（docker compose 构建目录）
│   ├── .env / raw / wiki / state / graph / reports / exports / data
│   │                                未跟踪数据，checkout 不覆盖
│   └── Dockerfile                    部署时重放阿里云镜像源
└── docker compose: wiki-dashboard + rpa-mock
```

## 4. 服务器首次接入

1. 创建 bare 仓库：`git init --bare /opt/pharma-cost-wiki.git`。
2. 在 `/opt/pharma-cost-wiki` 初始化临时 Git 仓库，提交当前代码为 `server-baseline`，推入 bare 仓库后删除工作区临时 `.git`。
3. `update.sh` 通过 `.git/info/exclude` 保留在服务器本地，不进入版本库。
4. 安装 `deploy/post-receive` 到 bare 仓库 `hooks/` 并 `chmod +x`。
5. 首次部署前记录 `.env` 与数据目录的文件数/校验和；部署后复核。

## 5. 本地配置

```bash
git remote add server root@8.155.135.176:/opt/pharma-cost-wiki.git
git config core.sshCommand "ssh -i F:/llm-wiki/wiki/wiki/id_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
```

`core.sshCommand` 只影响 SSH 远程，`origin` 仍是 HTTPS。

## 6. 发布流程

1. `git switch -c feat/<name> origin/main`。
2. 修改、验证。
3. 用户审阅并明确认可。
4. 提交功能分支。
5. `bash deploy/ship.sh feat/<name>`：
   - 校验工作区干净、私钥安全边界；
   - `git switch main` + `git merge --ff-only origin/main`；
   - `git merge --no-ff feat/<name>`；
   - `git push origin main`；
   - `git push server main`（触发钩子）；
   - 公网 `http://8.155.135.176:8765/api/health` 健康检查。

## 7. 钩子行为与失败处理

- 仅 `refs/heads/main` 触发；其他分支只入库。
- 记录 `deploy-prev` 后执行 `git checkout -f main`。
- 重放阿里云 apt/pip 镜像源。
- `docker compose up -d --build`，然后轮询 `/api/health` 最多约 2 分钟。
- 构建失败：旧容器继续运行，钩子退出非 0 并记录回滚命令。
- 健康检查失败：保留 `deploy-prev`，记录回滚命令。
- 钩子绝不执行 `git clean`，避免删除 `.env` 与业务数据。

## 8. 安全边界

- `id_ed25519` 在 `.gitignore` 与 `.dockerignore` 中显式忽略。
- `deploy/ship.sh` 推送前检查：未被跟踪、已被忽略、`.dockerignore` 已排除、已跟踪文件无私钥内容。
- 服务器钩子检查：main 中无 `BEGIN OPENSSH PRIVATE KEY`，工作区无 `id_ed25519*`。
- 修复 `.gitignore` 的行内注释问题：`wiki/ state/ graph/ reports/ exports/` 原规则实际未生效。
- `inbox/`、`.index/`、本地交付包加入忽略，避免误提交赛题数据。

## 9. 验收清单

- [ ] `git check-ignore -q id_ed25519` 返回成功。
- [ ] `git ls-files | grep id_ed25519` 为空。
- [ ] `git ls-tree -r server/main --name-only | grep id_ed25519` 为空。
- [ ] 服务器 `find /opt/pharma-cost-wiki -maxdepth 1 -name 'id_ed25519*'` 为空。
- [ ] 推送非 main 分支不触发构建。
- [ ] 推送 main 后容器重建，`/api/health` 返回 200。
- [ ] 部署前后 `.env`、`raw/`、`wiki/`、`state/`、`graph/`、`reports/`、`exports/`、`data/` 文件数与校验和一致。
- [ ] `deploy-prev` 存在且可回滚。

## 10. 已知取舍

- 自动部署 + 健康检查，不自动回滚：避免在故障时继续自动操作生产数据。
- 双推送：GitHub 作为异地备份；服务器作为部署源。
- 保留 legacy `deploy/update_server.sh`：应急手动更新路径，不作为主流程。
