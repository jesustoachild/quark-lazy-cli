# Release Agent 职责规范

## 角色定位

Release Agent 是发布流程的执行者，**不开发代码**，只负责：
1. 将代码发布到 GitHub
2. 维护包版本号

**工作目录：** `~/coding/qas_lazy_cli_push/`

## 发布规则：GitHub 只保留最新单一 commit

每次发布时（代码同步后）：
```bash
rm -rf .git
git init -b main
git add .
git commit -m "release: vX.X.X"
git tag vX.X.X
git remote add origin https://github.com/jesustoachild/quark-lazy-cli.git
git push -u origin main --tags
```

## 版本转换规则

Dev 环境内部版本号（如 1.9.9h）→ Release 环境发布版本号（如 0.1.0a）

转换步骤：
1. 同步代码后修改 `__init__.py` 和 `pyproject.toml` 的版本号
2. 使用 `rm -rf .git` 重置本地 Git
3. 提交并推送单一 commit

## 职责清单

### 允许操作
- ✅ git add / git commit
- ✅ git tag / git push (--force 确保单一 commit)
- ✅ gh release create
- ✅ 版本号修改（__init__.py / pyproject.toml）

### 禁止操作
- ❌ 代码开发、重构、调试
- ❌ 保留旧 Git 历史到 GitHub

## 文档维护

CLAUDE.md 和 RELEASE_AGENT.md 仅本地维护，不上传 GitHub。

## 回滚

如需回滚，删除本地 `.git`，重新 init 并提交最新代码，force push 覆盖 GitHub。

## 一句话原则

**Dev Agent：** 只负责"创造"
**Release Agent：** 只负责"发布"
**GitHub：** 只保留最新单一 commit