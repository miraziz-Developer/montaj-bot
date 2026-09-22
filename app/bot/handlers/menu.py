import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards as kb
from app.bot import texts
from app.core.config import Settings
from app.models.user import User
from app.services.billing import trial_available
from app.services.onboarding import trial_offered

logger = logging.getLogger(__name__)
router = Router(name="menu")
router.message.filter(F.chat.type == "private")


def menu_markup(user: User, settings: Settings):
    return kb.main_menu(kb.mini_app_url(settings), trial_offered(user))


async def send_menu(message: Message, user: User, settings: Settings) -> None:
    await message.answer(texts.MENU_TITLE, reply_markup=menu_markup(user, settings))


def balance_text(user: User) -> str:
    text = texts.BALANCE.format(units=user.balance_units)
    return text + (texts.BALANCE_TRIAL_LINE if trial_available(user) else "")


@router.message(Command("help"))
async def cmd_help(message: Message, user: User, settings: Settings) -> None:
    await message.answer(texts.HELP, reply_markup=menu_markup(user, settings))


@router.message(Command("balance"))
async def cmd_balance(message: Message, user: User) -> None:
    await message.answer(balance_text(user))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, user: User, settings: Settings) -> None:
    await state.clear()
    await message.answer(texts.CANCELED)
    await send_menu(message, user, settings)


@router.callback_query(F.data == kb.CB_BALANCE)
async def cb_balance(callback: CallbackQuery, user: User) -> None:
    await callback.answer()
    await callback.bot.send_message(callback.from_user.id, balance_text(user))
