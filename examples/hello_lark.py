"""hello_lark.py — pocket-cc M0/M1-C 飞书 API 烟测脚本.

用 pocket_cc.lark 的公开接口实现, 验证从 SDK 提炼出来的模块层可用:
  1. LarkEventLoop — WS 长连接订阅 (im.message.receive_v1 + card.action.trigger)
  2. LarkOapiClient — REST 发卡片 + PATCH 卡片
  3. lark.card — build_status_card / CardButton 模板

行为对照原版:
  - 收到任意消息 → 发一张 ⏳ 卡片 → 1.5s 后 PATCH → 再 1.5s 后 PATCH 成 ✅
  - 卡片按钮点击 → 终端打印 card.action 事件 (含 message_id / value)

用法:
    cp .env.example .env  # 填 LARK_APP_ID / LARK_APP_SECRET
    uv run python examples/hello_lark.py
"""

from __future__ import annotations

import os
import threading

from dotenv import load_dotenv

from pocket_cc.lark import (
    CardAction,
    CardButton,
    IncomingMessage,
    LarkApiError,
    LarkEventLoop,
    LarkOapiClient,
    build_status_card,
)

load_dotenv()

APP_ID = os.environ.get("LARK_APP_ID", "")
APP_SECRET = os.environ.get("LARK_APP_SECRET", "")
if not APP_ID or not APP_SECRET:
    raise SystemExit(
        "LARK_APP_ID / LARK_APP_SECRET not set. Copy .env.example to .env and fill them in."
    )

rest = LarkOapiClient(APP_ID, APP_SECRET)

PING_BUTTON = CardButton(
    text="👋 Ping me",
    value={"action": "ping", "step": "demo"},
    style="primary",
)


def _card(state: str, title: str, body: str) -> dict[str, object]:
    return build_status_card(
        title=title,
        body=body,
        state=state,  # type: ignore[arg-type]
        actions=[PING_BUTTON],
    )


def _schedule_patch(message_id: str, delay_s: float, card: dict[str, object], tag: str) -> None:
    def _patch() -> None:
        try:
            rest.patch_card(message_id, card)
            print(f"[patch_card {tag}] ok message_id={message_id}")
        except LarkApiError as e:
            print(f"[patch_card {tag}] FAILED {e}")

    threading.Timer(delay_s, _patch).start()


def on_message(msg: IncomingMessage) -> None:
    print(
        f"[message] chat={msg.chat_id} type={msg.message_type} "
        f"sender={msg.sender_open_id} text={msg.text!r}"
    )

    try:
        message_id = rest.send_card(
            msg.chat_id,
            _card("running", "pocket-cc M0 测试 1/3", "Step 1: 卡片已发送, 1.5s 后第一次 PATCH…"),
        )
    except LarkApiError as e:
        print(f"[send_card] FAILED {e}")
        return
    print(f"[message] card sent message_id={message_id}")

    _schedule_patch(
        message_id,
        1.5,
        _card("running", "pocket-cc M0 测试 2/3", "Step 2: 第一次 PATCH ✓, 1.5s 后再 PATCH 一次…"),
        "2/3",
    )
    _schedule_patch(
        message_id,
        3.0,
        _card(
            "done",
            "pocket-cc M0 测试 3/3",
            "Step 3: PATCH 三次完成 ✓\n\n点下面的按钮测试 card.action 回调。",
        ),
        "3/3",
    )


def on_card_action(action: CardAction) -> None:
    print(
        f"[card.action] sender={action.sender_open_id} message={action.message_id} "
        f"token={action.token} value={action.value}"
    )


def main() -> None:
    loop = LarkEventLoop(APP_ID, APP_SECRET)
    loop.on_message(on_message)
    loop.on_card_action(on_card_action)
    print("[hello_lark] WS connecting… send any message to the bot in Lark.")
    loop.start()


if __name__ == "__main__":
    main()
