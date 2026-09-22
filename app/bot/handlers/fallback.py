from aiogram import F, Router
from aiogram.types import Message

from app.bot import texts
from app.bot.handlers.menu import menu_markup
from app.core.config import Settings
from app.models.user import User

router = Router(name="fallback")
router.message.filter(F.chat.type == "private")


@router.message(F.video | F.document | F.video_note | F.animation)
async def video_in_chat(message: Message, user: User, settings: Settings) -> None:
    await message.answer(texts.NO_VIDEO_IN_CHAT, reply_markup=menu_markup(user, settings))


@router.message(F.text)
async def other_text(message: Message, user: User, settings: Settings) -> None:
    if not user.onboarding_completed:
        await message.answer(texts.NEED_START)
        return
    await message.answer(texts.HINT, reply_markup=menu_markup(user, settings))
