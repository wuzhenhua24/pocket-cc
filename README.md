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

到 [飞书开放平台](https://open.feishu.cn/app)（国际版 [Lark](https://open.larksuite.com/app)）创建企业自建应用，记下 **App ID** / **App Secret**，然后按下面 6 个开关全配 —— **缺一个 pocket-cc 都会半残**。

| # | 位置 | 配什么 | 不配的后果 |
|---|---|---|---|
| 1 | 应用功能 → **机器人** | 启用机器人能力 | bot 完全不可用 |
| 2 | 权限管理 → **API 权限** | 见下方权限清单 | 收不到 / 发不出消息 |
| 3 | 事件与回调 → **事件订阅** | 模式 = **长连接**；订阅 `im.message.receive_v1` | 收不到用户消息 |
| 4 | ⚠️ 事件与回调 → **回调** | 模式 = **长连接**（独立开关！） | 用户点卡片按钮报 **`200340`** |
| 5 | 版本管理与发布 | 创建版本 + 申请线上发布 | WS 连不上 / 401 |
| 6 | 飞书 App | 把 bot 拉进测试群，或搜应用名开私聊 | 没法测试 |

**第 4 项是最容易漏的坑**：飞书把「事件订阅」和「卡片回调」当成两套独立配置，必须**各自**切到长连接模式。

#### API 权限清单（第 2 项展开）

最小自用集合（单聊场景）：
- `im:message:send_as_bot`（**必填** — 以应用身份发消息）
- `im:message`（消息读写伞形权限）
- `im:message.p2p_msg` + `im:message.p2p_msg:readonly`（接收单聊消息）
- `cardkit:card:write`（**必填** — 创建卡片实体）
- `cardkit:card`（**必填** — 更新卡片实体；卡片流式追加、状态翻转都靠它）

群聊场景再加：
- `im:message.group_at_msg` + `im:message.group_at_msg:readonly`（群聊 `@bot` 消息）

M2-D 文件回传时再加：
- `im:resource`（上传/下载图片文件）

> 改完权限**必须**重新发版，否则旧 token 仍按旧 scope 集合鉴权。新 scope 没生效时第一条消息就报 `99991672` / `99991679`（创建卡片被拒）或 `99991680` / `99991681`（更新卡片被拒）。

完整、详细带「哪个功能依赖哪个权限点」的对照见 [`deploy/README.md` §5](./deploy/README.md#5-在飞书后台配应用)。

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

- **WS 连不上 / 401**：检查 APP_ID/SECRET 是否对；应用是否已发布；事件订阅模式是否开了长连接。
- **收不到消息**：API 权限是否勾对；事件订阅是否选了 `im.message.receive_v1`；bot 是否被拉进群（群聊场景）。
- **点卡片按钮报 200340**：上面第 4 项「卡片回调」没切到长连接 —— 它跟事件订阅是**独立开关**。
- **PATCH 失败**：99991668 = 消息不存在；230001 = 没权限改这条消息；230002 = 24h 之外不可编辑。

---

## 在飞书侧用 pocket-cc

### 卡片头部颜色 = Claude 状态

| 颜色 + emoji | 状态 | 含义 |
|---|---|---|
| 🟦 ⏳ | running | Claude 正在工作；内容在持续刷新 |
| 🟩 ✅ | done | Claude 完成；卡片封板（Stop hook 触发） |
| 🟥 ❌ | failed | Claude 异常结束（StopFailure / pocket-cc 内部错误） |
| 🟧 ❓ | waiting | Claude 在等你回应（permission prompt / AskUserQuestion 等） |

### Running 状态卡片底部 4 个按钮

每个按钮对应一个 tmux 按键序列发给真实的 Claude TUI：

| 按钮 | 实际发的 tmux 键 | 适用场景 |
|---|---|---|
| **⏹ 中断** | `C-c` + 200ms + `Escape`（**双连**） | **彻底停止当前任务**：break task → 退出 Claude 的「Interrupted · 你想改成什么？」redirect prompt → 清空输入框。**下次发新消息时不会拼接残留** |
| **⎋ Esc** | `Escape` + 100ms + `Escape`（**双发**） | **清空输入框 / 退当前 prompt**。Claude TUI 单 Esc 不彻底清，需要双发；从单按钮一次发完，避开飞书"操作太频繁"的连点频控 |
| **⇧⭾ Mode** | `BTab`（Shift-Tab） | 切换 Claude 权限模式（plan / acceptEdits / bypassPermissions），跟终端按 Shift-Tab 一样的效果 |
| **📜 内容** | （不发键）抓 `tmux capture-pane` 文本 → 飞书发新一条卡片 | 想看 Claude TUI 当前完整的屏幕内容（pocket-cc 投影漏的、ANSI 渲染细节等） |

### Waiting 状态卡片：选项按钮

当 Claude 弹出 permission prompt 或问选择题时，卡片变 ❓ 橙色，按钮换成：

| 按钮 | 发给 Claude | 说明 |
|---|---|---|
| **1. Yes / 2. No / …** | `"1"` / `"2"` / …（数字 + Enter） | 用 Claude TUI 的数字快捷键直接选。**前 4 个选项**有按钮；选项 ≥ 5 时在卡片 body 列出，飞书发数字也能响应（透传） |
| **⏹ 中断** | C-c + Esc 双连 | 同 running 状态 |
| **⎋ Esc** | Escape × 2（双发） | 取消 prompt + 清输入，同 running 状态 |

### 飞书直接发文字 = 直接发给 Claude

pocket-cc **零命令路由** —— 你发任何文字都原样 `send_text` 给 Claude TUI：
- 普通问题 → Claude 当 prompt 处理
- `/clear` / `/compact` / `/agents` 等斜杠命令 → 由 Claude 自己处理（pocket-cc 不截胡）
- waiting 状态下发文字 → 当作 prompt 回答（continuation 路径，不开新卡片）
- 单字符 `1` `2` 等 → waiting 状态下相当于按对应选项按钮

### 长内容自动续卡

单 turn 输出超过 ~2500 字符时，pocket-cc 自动 close 当前卡片（末尾「⏬ 内容续下条」），新发一张「(续) 原标题」卡片继续 patch。整段 Claude 回复完整保留，不丢早期内容。

---

## 生产部署（Linux）

详见 [`deploy/README.md`](./deploy/README.md) — 完整的服务器部署清单（装依赖、配置 .env、装 hooks、systemd 常驻、故障排查、升级 / 卸载）。

附带的 [`deploy/pocket-cc.service`](./deploy/pocket-cc.service) 是开箱即用的 systemd user unit 模板。

---

## 项目结构

```
pocket-cc/
├── pyproject.toml          # uv 管理
├── uv.lock                 # 锁文件（提交）
├── .python-version         # 3.12
├── DESIGN.md               # 设计文档
├── README.md
├── deploy/                 # Linux 部署文档 + systemd unit 模板
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
