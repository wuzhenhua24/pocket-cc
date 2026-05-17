"""hello_tmux.py — pocket-cc M1 切片 A 端到端验证.

验证 TmuxManager 的核心路径:
  1. ensure_session  — 创建 session (幂等)
  2. new_window      — 起 window 跑 bash
  3. send_text       — 发命令 "echo hello-from-pocket-cc"
  4. capture_pane    — 拿到 pane 文本, 应含输出字符串
  5. send_key        — 发 C-c (这里只是演示, bash 收到无副作用)
  6. kill_window     — 收尾
  7. kill_session    — 整个 session 清掉

用法:
    uv run python examples/hello_tmux.py

成功的标志: 末尾打印 "✅ all checks passed".
"""

from __future__ import annotations

import sys
import time

from pocket_cc.tmux import TmuxError, TmuxManager

SESSION = "pocket-cc-hello"
MARKER = "hello-from-pocket-cc"


def main() -> int:
    tmux = TmuxManager(SESSION)
    print(f"[1/7] ensure_session({SESSION!r})")
    tmux.ensure_session()
    # 幂等性测试: 再调用一次不应报错
    tmux.ensure_session()
    assert tmux.session_exists(), "session_exists should be True after ensure_session"

    print("[2/7] new_window(name='hello') with default shell")
    win = tmux.new_window(name="hello")
    print(f"      → window_id={win.window_id} name={win.name} cwd={win.cwd} pane={win.pane_id}")
    assert win.window_id.startswith("@"), f"unexpected window_id: {win.window_id}"
    assert win.pane_id.startswith("%"), f"unexpected pane_id: {win.pane_id}"
    assert win.session == SESSION

    # 起一下让 shell 进入提示符 (尤其 zsh 启动慢)
    time.sleep(0.5)

    print(f"[3/7] send_text('echo {MARKER}')")
    tmux.send_text(win.window_id, f"echo {MARKER}")
    # echo 后输出会很快, 但还是给一拍 buffer 时间
    time.sleep(0.8)

    print("[4/7] capture_pane")
    pane = tmux.capture_pane(win.window_id)
    print("      pane content (last 200 chars):")
    print("      " + repr(pane[-200:]))
    if MARKER not in pane:
        print(f"      ❌ MARKER {MARKER!r} not found in pane content")
        tmux.kill_session()
        return 1
    print(f"      ✓ marker {MARKER!r} present")

    print("[5/7] send_key('C-c')  (demo only — bash ignores it at prompt)")
    tmux.send_key(win.window_id, "C-c")
    time.sleep(0.2)

    print("[6/7] kill_window")
    tmux.kill_window(win.window_id)
    assert not tmux.window_exists(win.window_id), "window should be gone after kill"

    # find_window_by_name 还能找到原 session 自带的 placeholder 窗口
    remaining = tmux.list_windows()
    print(f"      remaining windows: {[w.name for w in remaining]}")

    print("[7/7] kill_session")
    tmux.kill_session()
    assert not tmux.session_exists(), "session should be gone after kill_session"

    print()
    print("✅ all checks passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TmuxError as e:
        print(f"❌ TmuxError: {e}")
        sys.exit(2)
