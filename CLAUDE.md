# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Role: Release Agent

Release Agent 工作目录：`~/coding/qas_lazy_cli_push/`

## 职责

- ✅ git push / git tag / gh release
- ✅ 代码质量检查
- ✅ GitHub Release 管理
- ✅ 包版本号维护（发布时转换）

- ❌ 代码开发、重构、调试

## 发布规则

**GitHub 只保留最新单一 commit。**

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

## 包信息

- **pip 包名**: quark-lazy-cli
- **短命令**: qslazy
- **GitHub 仓库**: https://github.com/jesustoachild/quark-lazy-cli
- **旧命令**: qas_lazy_cli, qlazy

## 版本转换

Dev 环境内部版本号（如 1.9.9h）→ Release 环境发布版本号（如 0.1.0a）

## 环境变量

| Variable | Required | Description |
|----------|----------|-------------|
| `QAS_HOST` | Yes | QAS 服务地址 |
| `QAS_API_TOKEN` | Yes | API token |
| `LAZY_CLI_ADVISOR` | No | Advisor mode (default: human) |
| `LAZY_CLI_DEBUG` | No | Debug mode (default: false) |

## 项目结构

```
src/quark_lazy_cli/
├── main.py     # CLI 入口
├── api.py      # QAS API 客户端
├── app.py      # 应用逻辑
├── advisor.py  # 顾问模式
├── config.py   # 配置管理
└── models.py   # 数据模型
```