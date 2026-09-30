"""Tools entry: Telegram toast, without navigation."""
from aiogram import F, types
from aiogram.exceptions import TelegramBadRequest
import userbot as ub

@ub.dp.callback_query(F.data == "tools")
async def tools_placeholder(callback: types.CallbackQuery):
    try:
        await callback.answer("🛠 Функция ещё в разработке!", show_alert=False, cache_time=0)
    except TelegramBadRequest:
        pass
