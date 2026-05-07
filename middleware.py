import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject

import database as db

logger = logging.getLogger(__name__)


class MasterContextMiddleware(BaseMiddleware):
    """
    Adds `data["master"]` for every update so handlers don't call db.get_master() manually.
    Also logs every user action for audit trail.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user:
            master = await db.get_master(user.id)
            data["master"] = master

            # Audit log
            if isinstance(event, Message) and event.text:
                logger.info("MSG  uid=%-12s text=%r", user.id, event.text[:60])
            elif isinstance(event, CallbackQuery):
                logger.info("CBQ  uid=%-12s data=%r", user.id, event.data)

        return await handler(event, data)


class BlockedUserMiddleware(BaseMiddleware):
    """Silently drops updates from users who are in lockout."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user:
            locked_secs = await db.get_lockout_seconds(user.id)
            if locked_secs > 0:
                # Only respond to messages (not callbacks) so the user sees the lockout message once
                if isinstance(event, Message):
                    mins = locked_secs // 60 + 1
                    await event.answer(
                        f"🚫 Аккаунт временно заблокирован. Попробуйте через {mins} мин."
                    )
                elif isinstance(event, CallbackQuery):
                    await event.answer(
                        f"🚫 Заблокировано. Подождите.", show_alert=True
                    )
                return  # drop the update
        return await handler(event, data)
