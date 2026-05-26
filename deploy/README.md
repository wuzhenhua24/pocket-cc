# pocket-cc Linux 部署指南

> 目标读者：在一台**内网 Linux 服务器**上把 pocket-cc 跑成常驻服务，把它跟你飞书机器人接起来。

适用：Ubuntu 22.04+ / Debian 12+ / RHEL 9+ / 其他主流发行版。

---

## 0. 前置条件

| | 说明 |
|---|---|
| **网络** | 服务器能访问 `open.feishu.cn:443`（出网）。**不需要**公网 IP / 端口映射 / HTTPS 证书 |
| **OS** | 任意主流 Linux，64-bit |
| **shell 权限** | 一个普通用户（**不需要 root** 运行 pocket-cc 本身；只在装系统依赖时需要 sudo） |
| **磁盘** | ~500 MB（uv venv + Claude Code workspace）|

---

## 1. 装系统依赖

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install -y tmux git curl

# RHEL / Fedora / Rocky
sudo dnf install -y tmux git curl
```

确认 tmux 版本 ≥ 3.0：
```bash
tmux -V   # tmux 3.x 或更新
```

---

## 2. 装 uv（Python 包管理器）

pocket-cc 用 [uv](https://github.com/astral-sh/uv) 管理 Python 依赖 — 它自带 Python 3.12 下载，**不需要**系统装 Python。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh

# 立刻让 PATH 生效（也可以重开 shell）
source "$HOME/.local/bin/env"

# 确认
uv --version
```

---

## 3. 装 Claude Code

pocket-cc 调用的是 `claude` CLI，所以必须先有 Claude Code 装在这台机器上。

```bash
# 推荐：用官方 npm 包
# 先装 Node.js（如果没有）
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs

# 装 Claude Code
npm install -g @anthropic-ai/claude-code

# 确认
claude --version
```

**首次启动 claude**（一次性配置 API key / 登录）：
```bash
claude
# 按提示完成登录
# 退出（Ctrl-C 或 /quit）
```

> ⚠️ pocket-cc 内部用 `claude` 这条命令启动 Claude Code。如果你装的不是这个 binary 名（比如自编译用 `claude-code`），改一下 `.env` 的 `POCKET_CC_CLAUDE_COMMAND`。

---

## 4. 拉 pocket-cc 代码

```bash
# 选个合适的目录
cd ~/
git clone https://github.com/<你的-account>/pocket-cc.git
cd pocket-cc

# 装依赖（uv 会自动建 .venv 并下载 Python 3.12）
uv sync
```

确认 CLI 能跑：
```bash
uv run pocket-cc --version
```

---

## 5. 在飞书后台配应用

到 [开放平台](https://open.feishu.cn/app) → 创建企业自建应用，记下 `LARK_APP_ID` / `LARK_APP_SECRET` 备用。然后按下表配置 **6 个开关**（缺一个 pocket-cc 都会半残）。

### 5.1 必开能力（「应用功能」侧栏）

| 位置 | 开关 | 作用 | 不开的后果 |
|---|---|---|---|
| 应用功能 → **机器人** | 「启用机器人能力」 | 让 bot 能被加好友、加群、发消息 | bot 完全不可用 |

### 5.2 API 权限（「权限管理 → API 权限」）

⚠️ 飞书改版后这个页面可能叫「开发配置 → 权限管理」，搜「权限」关键字。

逐项搜索并勾选：

| 权限点 | 作用 | 哪个功能依赖 |
|---|---|---|
| `im:message:send_as_bot` | 以应用身份发消息（**必填**） | 发文本 / 发卡片 / PATCH 卡片 |
| `im:message` | 消息读写伞形权限 | 上面 send_as_bot 的兜底 |
| `im:message.p2p_msg` | 接收单聊消息 | **单聊**收消息 |
| `im:message.p2p_msg:readonly` | 同上只读版本 | 飞书有时要求 readonly 配套勾上 |
| `im:message.group_at_msg` | 接收群聊 `@bot` 消息 | **群聊**场景才需要 |
| `im:message.group_at_msg:readonly` | 同上只读版本 | 群聊场景 |
| `im:resource`（可选） | 上传/下载文件、图片 | M2-D 文件回传时需要，M2 之前可不要 |

最小可用集合（自用单聊场景）：前三个 + p2p_msg:readonly = 4 个权限点。

### 5.3 事件订阅（「事件与回调 → 事件订阅」）

| 配置项 | 值 | 不对的后果 |
|---|---|---|
| **传输模式** | **长连接 (Websocket)** | 选 webhook 会要求公网 URL，pocket-cc 没法接 |
| 订阅事件 | `im.message.receive_v1`（接收消息 v1） | 收不到用户发的消息 |

> 飞书可能自动把 `im.message.message_read_v1`（消息已读）一起塞进来 —— pocket-cc 注册了 no-op handler 静默吃掉，不影响。

### 5.4 ⚠️ 卡片回调（**独立开关**，最容易漏！）

「事件与回调 → **回调**」（或老版本叫「机器人 → 消息卡片请求网址配置」）：

| 配置项 | 值 | 不对的后果 |
|---|---|---|
| **回调方式** / **传输模式** | **长连接 (Websocket)** | 用户点卡片按钮报 **`200340`** 错误（飞书去请求空 URL）|

> 这个开关跟上面的「事件订阅」是**两套独立配置** — 飞书把它们当成两个能力。你之前测试 M0 时点按钮报 200340 就是这里没切到长连接。

### 5.5 发布版本

| 步骤 | 说明 |
|---|---|
| 「版本管理与发布」→ 创建版本 | 填版本号 + 说明 |
| 「申请线上发布」 | 等管理员（自建应用通常就是你自己）审批通过 |

未发布的应用 WS 连不上，会报 401 / handshake failed。

### 5.6 加 bot 到测试 chat

发布通过后：
- **单聊**：手机/电脑飞书搜应用名（如 "pocket-cc"），打开私聊
- **群聊**：在测试群「设置 → 群机器人 → 添加机器人」选你的应用

### 5.7 自检：把 6 个开关从上到下过一遍

- [ ] 「机器人」能力已启用
- [ ] API 权限 4-7 个全勾上
- [ ] 事件订阅模式 = **长连接**
- [ ] 事件订阅列表含 `im.message.receive_v1`
- [ ] **卡片回调** 模式 = **长连接**（独立开关）
- [ ] 应用版本已发布

---

## 6. 配置 pocket-cc

```bash
cp .env.example .env
```

编辑 `.env`，**最少填**这两项：
```bash
LARK_APP_ID=cli_xxxxxxxxxxxxxxxx
LARK_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

其他可选项见 `.env.example` 注释。

### 6.1 配置授权用户（必填）— `~/.pocket-cc/users.toml`

白名单 + 每个用户的工作目录都写在这个 TOML 文件里。**至少要有一个用户条目**，否则启动会报错（`ConfigError: users config ... must define at least one [users.<open_id>] entry`）。

#### Step 1：先把每个用户的 workspace 目录建好

config 加载时会校验目录存在 + 可写 + 用户间互不包含；没建好会 ConfigError 启动失败。

```bash
# 共享目录（推荐）—— bot 进程的用户只要有 rwx 就行，不需要每个 user 各自的 unix 账号
sudo mkdir -p /home/linuxuser/workspace/alice
sudo mkdir -p /home/linuxuser/workspace/bob
sudo chown -R linuxuser:linuxuser /home/linuxuser/workspace
```

> 同一个用户的 workspace 之间**不能嵌套**（A 的目录是 B 的祖先 / 子孙都不行）—— 否则两边的 Claude 会污染同一份 `.claude/projects/` 缓存和 transcript。

#### Step 2：写 users.toml

```bash
mkdir -p ~/.pocket-cc
cat > ~/.pocket-cc/users.toml <<'EOF'
[users.ou_aaa111]
workspace = "/home/linuxuser/workspace/alice"
display_name = "alice"

[users.ou_bbb222]
workspace = "/home/linuxuser/workspace/bob"
display_name = "bob"
EOF
```

每个用户：
- key 是飞书 `open_id`（**怎么拿** → 见下面 6.1.1）
- `workspace`：该用户 Claude 的 cwd（绝对路径），必须是已存在、可写、且与其他用户互不包含的目录
- `display_name`：tmux window 命名 + 日志用，纯展示（出现在 `tmux a` 切换列表里）

> 修改 `users.toml` 必须重启 pocket-cc 进程才会生效（参见 §10「加 / 减用户」）。

#### 6.1.1 怎么拿到用户的 `open_id`？

最简单：**让用户先发一条消息**。

不在白名单里的用户发消息，bot 会直接回一条文本，里面就带着他自己的 `open_id`：

```
🚫 你不在 pocket-cc 白名单 — 请把下面这个 open_id 发给管理员加白：
ou_a1b2c3d4e5f6...
```

让用户把这条消息**整条转给你**，复制 `ou_...` 那行填进 `users.toml`，重启即可。同一个 `open_id` 在 60s 内重复发消息只会收到一次，不会被刷屏。

> 备选：在飞书后台 → 通讯录里查；但需要管理员权限，远不如让用户自己触发方便。

---

## 7. 装 Claude Code hooks（必须）

让 Claude 完成 / 失败时**瞬时**反映到飞书卡片（不靠下条消息触发）。

```bash
uv run pocket-cc hook install

# 验证
uv run pocket-cc hook status
# 应该看到 5 个事件都是 ✓:
# ✓  Notification
# ✓  SessionEnd
# ✓  SessionStart
# ✓  Stop
# ✓  StopFailure
```

hook 是装到 `~/.claude/settings.json` 的，幂等可重装。

> 多人提示：hook 是**机器级**的（属于 pocket-cc 进程的 unix 用户），所有用户的 Claude 共用同一份 hook 脚本和 `~/.pocket-cc/events.jsonl`。每条 hook 事件带 `session_id`，pocket-cc 内部按 session 配对回各自的 turn，不会串台。

---

## 8. 前台先跑一遍（验收）

```bash
# 注意：如果你的 shell 配了代理但代理走不通飞书，需要先 unset：
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY

uv run pocket-cc run
```

看到 `connected to wss://msg-frontier.feishu.cn/...` 就 OK，去飞书给 bot 发条消息，验证：
- 飞书侧出现「⏳ 运行中」卡片 → 流式刷出 Claude 工作内容
- Claude 完成后卡片瞬时变 ✅ 绿色
- 在另一终端 `tmux a -t pocket-cc` 能看到 Claude TUI 实时窗口

Ctrl-C 停掉，进入下一步常驻部署。

---

## 9. 用 systemd 常驻运行

pocket-cc 不需要 root。**用你自己的用户跑**，避免 Claude 写出来的文件归 root 所有。

### 9.1 装 unit 文件

把仓库的 [`pocket-cc.service`](./pocket-cc.service) 模板复制到 systemd 用户目录，按你的环境改：

```bash
mkdir -p ~/.config/systemd/user
cp deploy/pocket-cc.service ~/.config/systemd/user/pocket-cc.service

# 编辑：把 {{USERNAME}} / {{POCKET_CC_DIR}} 替换成你的实际值
vim ~/.config/systemd/user/pocket-cc.service
```

模板里需要替换的占位符：
| 占位符 | 改成 |
|---|---|
| `{{USERNAME}}` | 你的 Linux 用户名（`whoami` 看到的） |
| `{{POCKET_CC_DIR}}` | pocket-cc 仓库路径（如 `/home/zhenhua/pocket-cc`） |
| `{{UV_BIN}}` | uv 的绝对路径（`which uv` 看到的，通常 `/home/<user>/.local/bin/uv`） |

### 9.2 启用 + 启动

```bash
# 让 user systemd 在你登出后继续跑
loginctl enable-linger $(whoami)

systemctl --user daemon-reload
systemctl --user enable pocket-cc
systemctl --user start pocket-cc

# 看状态
systemctl --user status pocket-cc

# 看实时日志
journalctl --user -u pocket-cc -f
```

### 9.3 停 / 重启

```bash
systemctl --user stop pocket-cc       # 停（tmux session 里的 Claude 会被独立保留）
systemctl --user restart pocket-cc    # 重启
```

> 注意：`systemctl stop` **不会**杀掉 tmux session — Claude Code 继续在 tmux 里跑，只是飞书侧失联。重启后 pocket-cc 会从 `~/.pocket-cc/state.json` 把每个 binding（chat_id / open_id / cwd / window）自动 re-attach 回来，运行中的卡片会被打上「已重启」标记。restore 时也会用 `users.toml` 对账：被移出白名单或 workspace 改了路径的 binding 会被 drop + 杀掉对应 tmux window（详见 §11.6）。

---

## 10. 日常运维

| 任务 | 命令 |
|---|---|
| 查看 pocket-cc 日志 | `journalctl --user -u pocket-cc -f` |
| 接进 Claude 的 tmux 窗口看 | `tmux a -t pocket-cc`（多人时按 `Ctrl-B w` 出 window 列表，名字是 `chat-<display_name>`） |
| 看 hook 装好没 | `uv run pocket-cc hook status` |
| 看 events.jsonl | `tail -f ~/.pocket-cc/events.jsonl` |
| 看持久化 binding 状态 | `cat ~/.pocket-cc/state.json` |
| 当前授权用户列表 | `cat ~/.pocket-cc/users.toml` |
| 升级代码 | `git pull && uv sync && systemctl --user restart pocket-cc` |
| 全清干净（包括 tmux） | `systemctl --user stop pocket-cc && tmux kill-session -t pocket-cc` |

### 10.1 加用户（白名单扩容）

1. 让对方先发一条消息给 bot，复制回来 denial 里的 `open_id`
2. 给他建 workspace 目录并 chown 给 pocket-cc 跑的那个 unix 用户：
   ```bash
   sudo mkdir -p /home/linuxuser/workspace/charlie
   sudo chown -R linuxuser:linuxuser /home/linuxuser/workspace/charlie
   ```
3. 在 `~/.pocket-cc/users.toml` 加条目：
   ```toml
   [users.ou_ccc333]
   workspace = "/home/linuxuser/workspace/charlie"
   display_name = "charlie"
   ```
4. 重启：`systemctl --user restart pocket-cc`
5. 让对方再发一条消息，验证收到 ⏳ 卡片

### 10.2 撤销用户（白名单删除）

1. 从 `users.toml` 删掉对应 `[users.ou_xxx]` 段
2. `systemctl --user restart pocket-cc`
3. 重启时 restore 自动做这两件事（不用手工 kill window）：
   - 关掉那个用户对应的 tmux window（停掉里面跑着的 Claude）
   - 把他活动卡（如果有）patch 成「已重启」状态
4. 该用户再发消息会收到 denial 文本（且 60s 节流）

### 10.3 改用户的 workspace

1. 改 `~/.pocket-cc/users.toml` 里 `workspace = ` 值（先 mkdir + chown 新目录）
2. `systemctl --user restart pocket-cc`
3. restore 检测到 binding 持久化的 cwd ≠ config 新值 → drop binding + kill 旧 tmux window + patch 活动卡
4. 该用户下条消息会在**新目录**里新建 Claude session（相当于 `/clear` + cwd 切换）

> ⚠️ 切换 workspace 意味着丢掉旧目录的 Claude 上下文 —— 如果用户在旧目录里有未保存的工作，让他先存盘。

---

## 11. 故障排查

### 11.1 WS 连不上 (`timed out during opening handshake`)
- 检查能不能直连 `open.feishu.cn`：`curl -v https://open.feishu.cn`
- 如果走 SOCKS / HTTP 代理：把代理环境变量加进 systemd unit，或者让 `feishu.cn` 加进 `NO_PROXY`
- 检查应用是否已发布（飞书后台状态应该是「已发布」）

### 11.2 启动报 `LARK_APP_ID and LARK_APP_SECRET must be set`
- `.env` 在 pocket-cc 仓库根目录吗？systemd unit 的 `WorkingDirectory` 是否指向那里？

### 11.3 飞书卡片一直停在「运行中」
- 检查 hook 装好没：`uv run pocket-cc hook status`
- 检查 `~/.pocket-cc/events.jsonl` 是否有内容增长
- 看 pocket-cc 日志有没有 `Stop hook → sealing turn` 这种行

### 11.4 飞书 permission prompt 没出现卡片选项（应该有 ❓ + Yes/No 按钮）
- M2-C 已经做掉了 — 如果你的代码版本比 M2-C 早，更新拉最新
- 看日志有没有 pane_watcher 报错

### 11.5 用户点按钮报错 200340（卡片回调失败）
- 飞书应用「事件订阅 → 卡片回调」**单独**要开长连接（不是事件订阅那个开关复用）
- 详见根 README 排查段

### 11.6 启动报 `ConfigError: users config ...`
启动期 `users.toml` 校验失败的常见原因：
| 错误片段 | 原因 | 修法 |
|---|---|---|
| `users config not found` | 文件不存在 | 按 §6.1 建文件 |
| `must define at least one [users.<open_id>]` | 没有 `[users.ou_xxx]` 段 | 至少配一个用户 |
| `workspace is required` / `display_name is required` | 字段缺失或为空 | 两个字段都必填 |
| `workspace ... does not exist` | 目录不存在 | `mkdir -p` 那个路径 |
| `workspace ... is not writable` | 目录权限不对 | `chown` 给 pocket-cc 进程的 unix 用户 |
| `share workspace` / `workspaces ... overlap` | 两个用户指向同一目录 / 嵌套 | 改成互不包含的独立目录 |

### 11.7 同事说"我发消息没反应"
- 八成是没加白名单。让他给 bot 发一条消息，**bot 一定会回一条文本**（含他的 `open_id`），把整条消息转给你
- 如果他说"什么都没收到"：检查 (1) bot 是否被他加为好友、(2) `journalctl --user -u pocket-cc -f` 里有没有 `dropped non-p2p message`（说明他在群里发的，群聊被静默 ignore）

### 11.8 重启后日志里出现 `restore: open_id no longer in users config — dropping binding`
正常的撤销路径 —— 说明 `users.toml` 里删过这个用户，restore 自动清理了他遗留的 binding + tmux window。**不是 bug。**

同理 `restore: workspace changed in users.toml` 是切 workspace 的正常路径。

---

## 12. 安全建议

- **`users.toml` 维护好**：白名单 = users 表的 keys，没列进去的 `open_id` 一律拒收
- **workspace 隔离**：每个用户独立目录，不要指 `/` 或 `~`；config 加载时已经禁止用户间互相包含
- **不要用 root 跑 pocket-cc**：Claude Code 跑啥都用这个用户的权限
- **不要用 bypass 模式**（除非沙箱环境）：默认 `claude`（不带 `--permission-mode bypassPermissions`）让 Claude 该问就问，pocket-cc 会把 permission prompt 投到飞书让你确认
- **⚠️ 共享身份风险（多人场景必读）**：所有用户的 Claude 都跑在 pocket-cc 进程的同一个 unix 账号下，共享：
  - `~/.claude/` 里的登录态 / API key / skills / MCP server 配置
  - 装在 MCP server 里的外部凭证（lark-cli 的飞书身份、Gmail 身份、Drive 身份等）
  
  **后果**：用户 A 让 Claude 调一个外部工具（比如发邮件、改飞书文档）时，**它用的是机器主用户的身份在做事**，不是 A 自己的。小团队内部场景可接受；如果用户之间需要严格身份隔离，需要给每个用户单独配一个 `HOME=` 启动 `claude`（本仓库暂不支持，需要改 `POCKET_CC_CLAUDE_COMMAND` 启动逻辑）。
- **撤销要彻底**：从 `users.toml` 移除一个用户后**必须重启** pocket-cc —— 重启时 restore 会主动 kill 该用户的 tmux window，没重启的话他的 Claude 仍然在跑。

---

## 13. 升级 / 卸载

升级：
```bash
cd ~/pocket-cc
git pull
uv sync
uv run pocket-cc hook install    # 幂等
systemctl --user restart pocket-cc
```

卸载：
```bash
systemctl --user stop pocket-cc
systemctl --user disable pocket-cc
rm ~/.config/systemd/user/pocket-cc.service
uv run pocket-cc hook uninstall  # 移除 ~/.claude/settings.json 里的 pocket-cc 条目
tmux kill-session -t pocket-cc 2>/dev/null
rm -rf ~/.pocket-cc/             # 清掉 events.jsonl
rm -rf ~/pocket-cc/              # 删源码
```
