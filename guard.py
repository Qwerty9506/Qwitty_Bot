"""Guard module.

For now this module only owns the Guard callback so the feature can grow
independently without bloating the UserBot handlers.
"""

from userbot import dp, F, types


@dp.callback_query(F.data == "guard")
async def guard_placeholder(callback: types.CallbackQuery):
    await callback.answer()
