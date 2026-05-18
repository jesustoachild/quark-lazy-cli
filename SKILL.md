---
name: qslazy
description: Use this skill when the user wants an Agent to manage QAS-based Quark drive subscriptions for anime, dramas, or shows: install and configure qslazy, inspect subscription status, update missing/new episodes after user authorization, analyze release timing, suggest recurring update schedules, or handle complex resource selection with advisor modes.
---

# qslazy

## 1. 这个 Skill 是什么

qslazy 是给 Agent 使用的夸克网盘懒人追剧、追番 Skill。它内置 `qslazy` CLI，依赖 QAS（夸克网盘自动转存项目）后端。

Agent 通过它可以：

- 查看 QAS 订阅任务。
- 检查某部剧/番在网盘里的当前集数、缺集情况和更新目标。
- 搜索 QAS 后端提供的候选资源。
- 在用户授权后，转存新剧集或补全缺失剧集。
- 根据资源发布时间，帮助用户制定更合理的订阅更新时间。
- 在复杂资源场景下，用 `code`、`llm` 或 `agent` 顾问辅助选择资源。

不要把 qslazy 理解成普通下载工具。它更像 Agent 的“追剧订阅维护能力”：先观察订阅状态，再判断资源，再在用户授权范围内行动。

## 2. 安装和环境配置

安装分两步：**Skill 文档**（告诉 Agent 怎么用）和 **qslazy CLI**（真正干活的命令）。两个都要装。

### 第一步：安装 Skill 文档（OpenClaw Agent 专用）

```bash
openclaw skills install quark-lazy-cli
```

> `openclaw skills install` 只安装 SKILL.md 文档，不装 Python 包。

### 第二步：安装 qslazy CLI（必须）

**pipx 安装（推荐）**：适用 Linux 服务器 / 已具备 Python 3.10+ 和 pipx 的用户。

```bash
pipx install git+https://github.com/jesustoachild/quark-lazy-cli.git
```

**源码安装**：

```bash
git clone https://github.com/jesustoachild/quark-lazy-cli.git
cd quark-lazy-cli
pip install -e .
```

建议在 Skill 目录下保存配置：

```bash
cd <skill>
mkdir -p .quark-lazy-cli
cp .env.example .quark-lazy-cli/.env
```

最小 `.env`：

```env
QAS_HOST=http://your-qas-host:port
QAS_API_TOKEN=your-qas-api-token
LAZY_CLI_ADVISOR=code
LAZY_CLI_LOG_DIR=<skill>/.quark-lazy-cli/logs
LAZY_CLI_REPORT_DIR=<skill>/.quark-lazy-cli/report
LAZY_CLI_AGENT_MSG_DIR=<skill>/.quark-lazy-cli/message
```

配置优先级从高到低：

```text
命令行临时环境变量 > 系统环境变量 > --env 指定的 .env 文件 > 默认值
```

例如，下面命令临时使用 `code` 顾问，不会修改 `.env` 文件：

```bash
LAZY_CLI_ADVISOR=code qslazy task list --env <skill>/.quark-lazy-cli/.env
```

## 3. 快速上手和验证

先验证命令可用：

```bash
qslazy --help
qslazy task --help
qslazy update --help
```

再建立对用户订阅的第一感觉：

```bash
qslazy task list --env <skill>/.quark-lazy-cli/.env
```

查看单个订阅状态：

```bash
qslazy task status 凡人修仙传 --env <skill>/.quark-lazy-cli/.env
```

`task status` 是 Agent 的第一入口。它会告诉 Agent：

- 网盘中已有的最高集、最低集和缺集情况。
- 下一次更新的目标集数。
- 搜索到的新鲜资源和旧资源。
- 候选资源的发布时间。

示例输出：

```text
[ 凡人修仙传 ] Searching for 凡人修仙传...

============================================================
  任务：凡人修仙传
  网盘：最高 153（更新于：今天），最低 1，无缺集
  目标：第153集以上资源
  偏好：最高画质优先

============================================================
  搜索到 4 个7天内新鲜资源，0 个旧资源

  [1] [昨天: 20:30 周六] 凡人修仙传 4K 高码率 更新至153集
  [2] [昨天: 21:10 周六] 凡人修仙传 4K HDR 更新至153集
  [3] [昨天: 22:30 周六] 凡人修仙传 4K DV 更新至153集
  [4] [昨天: 23:05 周六] 凡人修仙传 S01E153 2160p WEB-DL
```

读取要点：

- `网盘：最高 153`：本地已存在的最高集数。
- `最低 1，无缺集`：本地从第 1 集到最高集没有明显缺口。
- `目标：第153集以上资源`：更新时会寻找高于当前最高集的资源。
- `新鲜资源`：搜索有效期内发布的资源，适合判断近期更新规律。
- `[1] [2] [3]`：候选资源序号。Agent 顾问模式写 JSON 时使用这些 1-based 序号。

## 4. 和用户确认日常使用场景

Agent 不要一上来就更新。先确认用户这次要的是“只看状态”“按需更新”，还是“全面接管订阅维护”。

### 按需行动

用户说：“帮我看看《凡人修仙传》的订阅情况。”

Agent 应执行：

```bash
qslazy task status 凡人修仙传 --env <skill>/.quark-lazy-cli/.env
```

然后根据 stdout 汇报：

```text
《凡人修仙传》当前网盘最高到第 153 集，暂无明显缺集。
搜索结果里有 4 个 7 天内的新鲜资源，发布时间集中在周六 20:30 到 23:05。
当前看起来订阅状态正常，可以继续观察；如果你希望我更新，我再执行转存。
```

用户说：“看看我《凡人修仙传》可能失败了，帮我更新下。”

Agent 应先观察：

```bash
qslazy task status 凡人修仙传 --env <skill>/.quark-lazy-cli/.env
```

如果 stdout 显示有新资源或缺集，再向用户确认：

```text
我看到《凡人修仙传》可能需要更新/补漏。是否现在执行 `update all`，补全缺集并追新？
```

用户确认后再执行：

```bash
LAZY_CLI_ADVISOR=code qslazy update all 凡人修仙传 --env <skill>/.quark-lazy-cli/.env
```

用户明确说“直接更新《凡人修仙传》”时，可以执行：

```bash
LAZY_CLI_ADVISOR=code qslazy update all 凡人修仙传 --env <skill>/.quark-lazy-cli/.env
```

但执行后仍然要只根据 stdout 汇报结果，不要默认读取 log 或报告文件。

### 全面接管订阅维护

用户说：“帮我接管订阅维护”“帮我制定定时更新计划”“帮我看看所有剧什么时候更新合适。”

Agent 应按这个流程：

1. 运行 `qslazy task list` 获取订阅清单。
2. 对每个用户关心的剧运行 `qslazy task status 剧名`。
3. 从 stdout 记录每部剧的网盘状态、缺集情况、候选资源发布时间。
4. 根据发布时间推算推荐订阅时间。
5. 先问用户偏好，再配置定时任务。

配置定时前必须询问：

```text
你希望订阅更新时间偏向哪种策略？
1. 时间优先：按最早有效资源发布时间 + 4 小时安排，尽快看到更新。
2. 画质优先：按最后一批高质量资源发布时间 + 4 小时安排，等待高码率、HDR/DV、字幕组或更完整资源。
```

如果用户没有说明，默认建议：

- 主策略：画质优先。
- 兜底策略：次日上午再重试一次。

以上方《凡人修仙传》为例：

- 最早有效资源：周六 20:30。
- 最后一批高质量资源：周六 22:30 到 23:05。
- 时间优先建议：周日 00:30。
- 画质优先建议：周日 02:30 到 03:05。
- 兜底重试建议：周日 09:00。

OpenClaw 定时任务示例：

```bash
openclaw cron add \
  --name "凡人修仙传-定时更新" \
  --cron "30 2 * * 0" \
  --tz "Asia/Shanghai" \
  --session isolated \
  --agent AgentName \
  --announce \
  --channel feishu \
  --account bot-AgentName \
  --to "USER_OPEN_ID" \
  --message "bash -c 'LAZY_CLI_ADVISOR=code qslazy update all 凡人修仙传 --env <skill>/.quark-lazy-cli/.env'\n\n仅根据 stdout 汇报结果，除非排错，不要读取 log 或报告文件。"
```

定时任务汇报建议包含：

```text
订阅：
当前：
目标：
偏好：
更新结果：
新增集数：
补全剧集：
```

## 5. 进阶场景

### 复杂资源筛选：`--add-prompt`

当资源存在多版本、多编号、多字幕组或排除条件时，使用 `--add-prompt` 明确选择规则。

```bash
LAZY_CLI_ADVISOR=llm qslazy update all 凡人修仙传 \
  --env <skill>/.quark-lazy-cli/.env \
  --add-prompt "只选择 S03E01 重新编号格式，不选择 EP 累计格式；优先 4K 高码率，其次 WEB-DL。"
```

常见提示：

| 场景 | `--add-prompt` 示例 |
|------|---------------------|
| 特定画质 | `优先选择 4K DV 或 HDR，不选择 1080p` |
| 特定字幕 | `优先选择内封中字资源` |
| 编号冲突 | `只选 S03E01 重新编号格式，不选 EP 累计格式` |
| 排除版本 | `排除国配版本，排除合集压缩包` |
| 体积偏好 | `在集数完整的前提下，优先选择较小体积版本` |

### Advisor 模式

`LAZY_CLI_ADVISOR` 决定资源选择方式。

| 模式 | 适合场景 | Agent 行为 |
|------|----------|------------|
| `human` | 用户手动确认 | 让用户看候选资源并选择 |
| `code` | 命名规范、集数清晰、资源简单 | 默认推荐，用规则自动选择 |
| `llm` | 多版本、画质/字幕/编号偏好复杂 | 加 `--add-prompt` 说明偏好 |
| `agent` | 外部 Agent 需要亲自判断候选资源 | 启动 CLI，读取 stdout，写 JSON 决策文件 |

LLM 顾问配置仅在 `LAZY_CLI_ADVISOR=llm` 时需要：

```env
LAZY_CLI_LLM_OPENAI_API_BASE=https://api.example.com/v1
LAZY_CLI_LLM_OPENAI_API_KEY=your-api-key
LAZY_CLI_LLM_OPENAI_MODEL=your-model-name
```

### Agent 顾问模式

`LAZY_CLI_ADVISOR=agent` 用于让外部 Agent 自己判断候选资源。CLI 会在 stdout 输出候选资源列表和一个 `advisor__任务名__时间戳.json` 文件路径。

启动时建议使用未缓冲输出：

```bash
PYTHONUNBUFFERED=1 LAZY_CLI_ADVISOR=agent qslazy update all 剧名 --env <skill>/.quark-lazy-cli/.env
```

Agent 看到决策文件路径后，写入 JSON：

```json
{"selected": [1, 2, 3, 4], "max_ep": 80}
```

字段含义：

- `selected`：选择的候选资源序号，使用 stdout 中的 1-based 序号。
- `max_ep`：Agent 根据候选标题判断出的最新集数。

注意：

- 不要向 stdin 输入选择。
- 必须结合任务目标和候选资源标题判断。
- 5 分钟内没有写入决策文件，CLI 会超时。

Hermes Agent 和 OpenClaw 核心原理相同，只是工具名和语法不同：

| 步骤 | Hermes Agent | OpenClaw |
|------|--------------|----------|
| 启动 CLI | `terminal(background=true, pty=true, command="PYTHONUNBUFFERED=1 ...")` | `exec(command, pty=True, yieldMs=120000)` |
| 等待/轮询 | `process(wait)` / `process(poll)` | `process(action="poll", sessionId=session, timeout=...)` |
| 写决策文件 | 手动写 `advisor__任务名__时间戳.json` | `open(...)` + `json.dump(...)` |
| 超时 | 5 分钟 | 5 分钟（20 × 15s） |

### 复杂环境设置

如果用户有多个 QAS 后端、多个 Agent 或多个 Skill 安装位置，不要依赖默认路径。每次命令都显式传入：

```bash
--env <skill>/.quark-lazy-cli/.env
```

如果只想本次覆盖顾问模式，使用命令行临时环境变量：

```bash
LAZY_CLI_ADVISOR=llm qslazy update all 凡人修仙传 --env <skill>/.quark-lazy-cli/.env
```

## 6. 关键铁律

- **永远先用 `task status` 观察**，不要一上来就更新
- **未获授权不更新**。用户说“看看”“检查”时只跑 `task list/status`
- **只根据 stdout 汇报结果**，除非排错或用户要求，不读 log/报告文件，浪费时间和Token
- **不暴露 token**。不把真实 QAS API Token 写进示例、回复、日志
- **不擅自改动网盘文件**。不删除、改名、移动用户文件
- `task status` 显示目录访问失败、savepath 不存在时，向用户说明风险
- 搜索结果为空时，说明没有有效新资源，可建议稍后重试
- 资源命名混乱时，不用 `code` 硬选，改用 `llm` 或 `agent`
