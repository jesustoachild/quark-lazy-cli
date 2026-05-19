# 夸克网盘懒人追剧、追番 SKILL

基于 QAS（夸克网盘自动转存项目）的懒人追剧、追番 SKILL，也内置了一个 CLI 工具。即使 QAS 的剧集订阅失效，它也可以重新搜索资源、探查多级资源目录，并在多个最新候选资源中按用户偏好（如更高清晰度）转存新剧集和缺失剧集。

可以交给本地 OpenClaw、Hermes Agent 或其他 Agent 使用，用来设置定时任务、定时汇报，并在订阅失败时辅助修复。
有 Python 运行环境的进阶用户，也可以把它当作命令行工具使用。

## 核心需求：

- QAS 的订阅时常会失效<br>
  手工查询、维护订阅任务很麻烦。等忙完想起来再去找新资源时，资源可能已经失效了。

- Infuse、Emby 的海报墙被重复剧集搞乱<br>
  分享资源内容可能重复，例如：凡人修仙传.S05E01.4K.mkv、凡人修仙传.S05E01.dv.mkv。用正则模板转存时，可能会把两个文件都转存下来，造成剧集文件混乱，让 Infuse、Emby 等媒体库的海报墙乱掉，需要手动删除、改名和维护。

- 喜欢最高视频编码的资源<br>
  如果偏好最高视频编码的资源，或希望选择较小体积的视频，通常需要手动下探搜索结果目录、挑选和比对。

- Agent 来了，自己更懒了<br>
  让 Agent 忙起来，自己安心当懒人。

## 项目甜点：

- 再也不操心订阅失效<br>

- 节省 Token。命名规范、发布清晰的分享资源，可以交给 `code` 顾问执行；复杂资源情况则交给 Agent 或 LLM 大模型判断和决策<br>

- Agent 可以使用 SKILL 分析资源的发布规律，并配置合理的订阅更新时间<br>

- 多资源匹配用户需求、精准指定转存<br>

---

## 使用概览

### 1. 手动追剧更新

```bash
# 追新：只转存高于当前最高集的新资源
qslazy update new 凡人修仙传

# 全量：补漏 + 追新
qslazy update all 凡人修仙传
```

### 2. 查看订阅状态

```bash
qslazy task list
qslazy task status 凡人修仙传
```

`task status` 会展示本地网盘状态、缺集情况、搜索到的资源发布时间，适合用来判断订阅是否正常，也可以帮助 Agent 推算最佳订阅更新时间。

### 3. Agent / OpenClaw / Hermes 用法

本项目提供 [SKILL.md](./SKILL.md)，包含更详细的 Agent 操作说明：

- 如何查看订阅任务和单个剧集状态
- 如何根据资源发布时间制定最佳订阅更新时间
- `LAZY_CLI_ADVISOR=agent` 的决策文件工作流
- Hermes Agent / OpenClaw 的后台执行与轮询方式
- `--add-prompt` 的复杂资源筛选用法

详细 Agent 用法请阅读 [SKILL.md](./SKILL.md)。

---

## 2. 安装和环境配置

安装分两步：**Skill 文档**（告诉 Agent 怎么用）和 **qslazy CLI**（真正干活的命令）。两个都要装。

### 第一步：安装 Skill 文档

skills.sh / 通用 Agent：

```bash
npx skills add jesustoachild/quark-lazy-cli
```

OpenClaw / ClawHub：

```bash
openclaw skills install quark-lazy-cli
```

> Skill 安装只安装 SKILL.md、示例配置和辅助资源，不安装 Python 包。

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

## 快速开始
- 让你的 OpenClaw Agent 完成配置<br>
- 或者，有 Python 运行环境的进阶用户可以手动配置和使用

### 1. 配置环境

```bash
cd /<skill>/quark-lazy-cli
cp .env.example .env
# 编辑 .env
```
详细环境变量配置，请阅读 [SKILL.md](./SKILL.md)。


### 2. 查看订阅

```bash
qslazy task list
```

### 3. 查看订阅任务状态

```bash
qslazy task status 凡人修仙传
```
---

### 4. 追更新集

```bash
qslazy update new 凡人修仙传
```

### 5. 补全缺集 + 追新

```bash
qslazy update all 凡人修仙传
```


## 许可证

MIT
