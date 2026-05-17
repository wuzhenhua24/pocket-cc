# pocket-cc

> 在手机飞书上获得**接近 Claude Code 官方 remote 模式**的体验 —— 远程终端里 Claude Code 实际跑什么、长什么样、当前在做什么，飞书界面就如实呈现什么。

详细设计：见 [DESIGN.md](./DESIGN.md)。

---

## 当前状态

**M1 切片 D — 端到端最小可用。** `pocket-cc run` 已就绪，可以在飞书里给 bot 发消息，看到 Claude 在远程跑、卡片流式刷新。

进度对照 [DESIGN.md §9](./DESIGN.md#9-分阶段-milestone)：
- [x] M0：飞书 API 调研（hello_lark.py 5 项验证）
- [x] M1-A：tmux 子包（subprocess 路线）
- [x] M1-B：Claude transcript 增量解析
- [x] M1-C：飞书客户端 + 卡片 + WS 事件包装
- [x] M1-D：relay 层 + bootstrap + `pocket-cc run`
- [ ] **(需要你)** 端到端验收
- [ ] M1-E：装 Claude hooks（让"完成"瞬时感知，不再靠 transcript 轮询）
- [ ] M2：交互式 UX（AskUserQuestion / Plan / Permission）

### M1 端到端验收清单

前置：tmux 已装；`.env` 里有 `LARK_APP_ID` / `LARK_APP_SECRET`；网络能访问 `open.feishu.cn`（如有 SOCKS 代理需在跑命令前 `unset all_proxy http_proxy https_proxy`）。

```bash
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
uv run pocket-cc run
```

启动后看到：
```
pocket-cc starting…
  tmux session  : pocket-cc
  workspace     : /Users/you/.pocket-cc/workspace
  claude command: claude --permission-mode bypassPermissions
  whitelist     : open (any user)
  ...
[INFO] pocket_cc.lark.event_loop: WS connecting ...
```

在飞书里给 bot 发消息（任意），按以下检查：

| # | 操作 | 预期 |
|---|---|---|
| 1 | 发 `"请用 ls -la 列出当前目录"` | 立刻收到一张 ⏳ 蓝色卡片（"运行中…"），1-2s 后开始 PATCH 出 Claude 的工作进展 + 工具调用列表 |
| 2 | 在另一个终端 `tmux a -t pocket-cc` | 能看到一个名为 `chat-xxxxxxxx` 的 window，里面是真实的 Claude Code 在跑 |
| 3 | 等 Claude 完成 | 卡片最终内容稳定（M1 阶段卡片不会自动变绿，因为还没装 hooks —— M1-E 修） |
| 4 | 再发 `"再列下 tests 目录"` | 新一张卡片，上一张保持原样 |
| 5 | 发 `"/clear"` | 直接透传给 Claude，Claude 自己处理（卡片可能没什么内容刷出来，但 tmux 里 Claude 已 clear） |
| 6 | 点卡片上 ⏹ 中断 | tmux 里 Claude 收到 Ctrl-C |
| 7 | 点 📜 内容 | 新发一张卡片，包含当前 pane 的最后 ~2000 字符 |
| 8 | Ctrl-C 停 `pocket-cc run` | 所有 active turn 卡片被最后 PATCH 一次后退出 |

跑通这 8 项 = M1 主线验收。任何卡壳贴日志一起看。

---

## 工具链

| | |
|---|---|
| 包管理 / 虚拟环境 / 锁文件 | [uv](https://github.com/astral-sh/uv) — 唯一包管理器，不混用 pip / poetry / pdm |
| Python | 3.12（`.python-version` 锁定） |
| 构建 | `hatchling` |
| 格式 / lint | `ruff` |
| 类型检查 | `mypy --strict` |
| 测试 | `pytest` + `pytest-asyncio`（`asyncio_mode = "auto"`） |

### 常用命令

```bash
uv sync                              # 安装/同步依赖到 .venv
uv run pocket-cc --version           # 跑 CLI
uv run python examples/hello_lark.py # 跑 M0 验证脚本
uv run ruff format .
uv run ruff check .
uv run mypy src/pocket_cc
uv run pytest
uv add <pkg>                         # 加运行依赖
uv add --dev <pkg>                   # 加开发依赖
uv lock --upgrade                    # 升级锁文件
```

`uv.lock` 提交到仓库。

---

## M0 设置步骤（你需要操作的）

### 1. 飞书自建应用申请

1. 打开 [飞书开放平台](https://open.feishu.cn/app)（国际版用 [Lark](https://open.larksuite.com/app)），登录。
2. 「创建企业自建应用」，填名称（如 `pocket-cc`）、图标、简介。
3. 在「凭证与基础信息」记下 **App ID** 和 **App Secret**。
4. 在「权限管理」→「机器人」开启 **添加机器人能力**。
5. 在「权限管理」→「API 权限」勾选：
   - `im:message`（发消息）
   - `im:message.group_at_msg` / `im:message.group_at_msg:readonly`（群聊收 @）
   - `im:message.p2p_msg` / `im:message.p2p_msg:readonly`（单聊收消息）
   - `im:message:send_as_bot`（以应用身份发消息）
6. 在「事件与回调」→「事件订阅」**模式选「长连接」**（不是 webhook），订阅事件：
   - `im.message.receive_v1`（接收消息）
   - `card.action.trigger`（卡片按钮回调）
7. 「版本管理与发布」→ 创建版本 → **申请线上发布**（自建应用需要管理员批准）。
8. 把机器人拉进一个测试群，或在飞书里搜应用名「pocket-cc」开私聊。

### 2. 配置本地环境

```bash
cp .env.example .env
# 编辑 .env，填入第 1 步拿到的 LARK_APP_ID / LARK_APP_SECRET
```

### 3. 跑 M0 验证脚本

```bash
uv run python examples/hello_lark.py
```

终端输出 `[hello_lark] WS connecting…` 后，到飞书里给 bot 发一条消息（任意内容）。

**预期看到**：
- 终端日志：`[message] chat_id=oc_xxx ...`
- 飞书里收到一张交互卡片，1.5s 内 PATCH 成 "2/3"，再 1.5s 后 PATCH 成 "✅ 3/3"
- 点卡片上的 `👋 Ping me` 按钮 → 终端日志：`[card.action] open_id=ou_xxx token=... value={'action': 'ping', 'step': 'demo'}`

5 项能力（DESIGN.md §7）全部走通 = M0 通过，进入 M1。

### 常见问题

- **WS 连不上 / 401**：检查 APP_ID/SECRET 是否对；应用是否已发布；长连接模式是否开启。
- **收不到消息**：检查 API 权限是否勾对、事件订阅是否选了 `im.message.receive_v1`、bot 是否被拉进群（群聊场景）。
- **PATCH 失败**：99991668 是消息不存在；230001 是没权限改这条消息；230002 是 24h 之外不可编辑（M0 阶段不会触发）。

---

## 项目结构

```
pocket-cc/
├── pyproject.toml          # uv 管理
├── uv.lock                 # 锁文件（提交）
├── .python-version         # 3.12
├── DESIGN.md               # 设计文档
├── README.md
├── examples/
│   ├── hello_lark.py       # 飞书 API 烟测
│   ├── hello_tmux.py       # tmux 烟测
│   └── parse_transcript.py # transcript 解析演示
├── src/pocket_cc/
│   ├── cli.py              # pocket-cc run / hook (M1-E)
│   ├── app/                # 应用层
│   │   ├── config.py       # .env 配置
│   │   ├── persistence.py  # chat ↔ window 绑定
│   │   └── bootstrap.py    # Pocketcc 主对象
│   ├── lark/               # 飞书层
│   │   ├── client.py       # LarkClient Protocol + OAPI + Fake
│   │   ├── card.py         # 卡片模板
│   │   └── event_loop.py   # WS 订阅
│   ├── tmux/               # tmux subprocess 包装
│   ├── claude/             # Claude Code 集成
│   │   ├── transcript.py   # JSONL 增量解析
│   │   └── session_index.py # cwd → active transcript
│   └── relay/              # IM ↔ Claude 转发
│       ├── input.py        # 入站消息/按钮 → tmux
│       ├── output.py       # transcript poller thread
│       ├── card_renderer.py # Event → card dict
│       └── card_stream.py  # 节流 PATCH
└── tests/
    ├── unit/               # 91 cases，纯 fake，~3s
    └── integration/        # 10 cases，真 tmux，~3s
```

---

## 与 ccgram 的关系

仓库里同目录的 `ccgram/`（已 gitignore）是 [alexei-led/ccgram](https://github.com/alexei-led/ccgram) 的源码，作为**参考实现**对照查看。pocket-cc 不依赖 ccgram、不 vendored、不 import；只是借鉴 tmux 路线 / hooks 用法 / transcript 增量读策略 / Protocol-based 客户端解耦等思路。详见 [DESIGN.md §11](./DESIGN.md#11-与-ccgram-的关系明确边界)。
