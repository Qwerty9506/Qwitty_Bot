from userbot import dp, F, types


@dp.callback_query(F.data == "guard")
async def guard_placeholder(callback: types.CallbackQuery):
    await callback.answer("Функция в разработке 🛠", show_alert=False, cache_time=0)
