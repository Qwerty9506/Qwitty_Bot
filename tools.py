"""Qwitty Tools section.

Keep every current and future Tools feature in this module so Account Manager
(userbot.py) only owns the root-menu entry button and never accumulates Tools
handlers/logic.
"""
from aiogram import F, types
from aiogram.utils.keyboard import InlineKeyboardBuilder

import userbot as ub

bot, dp = ub.bot, ub.dp


@dp.callback_query(F.data == "tools")
async def tools_placeholder(callback: types.CallbackQuery):
    if not callback.message or callback.message.chat.id != callback.from_user.id:
        try:
            await callback.answer("Откройте бота в личном чате.", show_alert=True)
        except Exception:
            pass
        return

    uid = callback.from_user.id
    ub.get_user_state(uid)["state"] = "TOOLS"

    builder = InlineKeyboardBuilder()
    builder.button(text="Назад в главное меню 🏠", callback_data="root_menu")
    builder.adjust(1)

    await ub.edit_or_send(
        uid,
        "<b>⛓️‍💥 Tools</b>\n<i>Раздел пока в разработке 🛠</i>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    try:
        await callback.answer()
    except Exception:
        pass
