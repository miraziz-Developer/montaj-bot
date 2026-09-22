import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.handlers.menu import send_menu
from app.bot.states import Onboarding
from app.core.config import Settings
from app.models.user import User
from app.services import onboarding as svc
from app.services.billing import trial_available
from app.services.users import set_onboarding

logger = logging.getLogger(__name__)
router = Router(name="start")
router.message.filter(F.chat.type == "private")

_NOT_A_COMMAND = ~F.text.startswith("/")


async def _say(bot: Bot, chat_id: int, text: str, **kwargs) -> None:
    await bot.send_message(chat_id, text, **kwargs)


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    user_created: bool,
    settings: Settings,
) -> None:
    await state.clear()
    await svc.apply_referral(session, user, user_created, svc.parse_referral_code(command.args))
    if user.onboarding_completed:
        await send_menu(message, user, settings)
        return
    await message.answer(
        texts.WELCOME.format(name=user.first_name or texts.DEFAULT_NAME), reply_markup=kb.welcome_kb()
    )


@router.callback_query(F.data == kb.CB_ONB_START)
async def onb_start(callback: CallbackQuery, user: User) -> None:
    await callback.answer()
    if not user.onboarding_completed:
        await _say(callback.bot, callback.from_user.id, texts.ASK_NICHE, reply_markup=kb.niche_kb())


@router.callback_query(F.data.startswith(kb.CB_NICHE))
async def onb_niche(callback: CallbackQuery, state: FSMContext, user: User) -> None:
    await callback.answer()
    key = callback.data.removeprefix(kb.CB_NICHE)
    if user.onboarding_completed or key not in svc.NICHE_KEYS:
        return
    if key == "other":
        await state.set_state(Onboarding.niche_text)
        await _say(callback.bot, callback.from_user.id, texts.ASK_NICHE_OTHER)
        return
    await state.update_data(niche=key)
    await _say(callback.bot, callback.from_user.id, texts.ASK_PURPOSE, reply_markup=kb.purpose_kb())


@router.message(Onboarding.niche_text, F.text, _NOT_A_COMMAND)
async def onb_niche_text(message: Message, state: FSMContext) -> None:
    niche = svc.clean_free_text(message.text)
    if niche is None:
        await message.answer(texts.ASK_NICHE_OTHER)
        return
    await state.set_state(None)
    await state.update_data(niche=niche)
    await message.answer(texts.ASK_PURPOSE, reply_markup=kb.purpose_kb())


async def _finish_onboarding(
    bot: Bot, chat_id: int, state: FSMContext, session: AsyncSession, user: User, purpose: str
) -> None:
    niche = (await state.get_data()).get("niche")
    if not niche:  # FSM data lost (e.g. Redis flushed): restart the questions
        await _say(bot, chat_id, texts.ASK_NICHE, reply_markup=kb.niche_kb())
        return
    await set_onboarding(session, user, niche, purpose)
    await state.set_state(Onboarding.contact)
    await _say(bot, chat_id, texts.ASK_CONTACT, reply_markup=kb.contact_kb())


@router.callback_query(F.data.startswith(kb.CB_PURPOSE))
async def onb_purpose(callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
    await callback.answer()
    key = callback.data.removeprefix(kb.CB_PURPOSE)
    if user.onboarding_completed or key not in svc.PURPOSE_KEYS:
        return
    if key == "other":
        await state.set_state(Onboarding.purpose_text)
        await _say(callback.bot, callback.from_user.id, texts.ASK_PURPOSE_OTHER)
        return
    await _finish_onboarding(callback.bot, callback.from_user.id, state, session, user, key)


@router.message(Onboarding.purpose_text, F.text, _NOT_A_COMMAND)
async def onb_purpose_text(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    purpose = svc.clean_free_text(message.text)
    if purpose is None:
        await message.answer(texts.ASK_PURPOSE_OTHER)
        return
    await _finish_onboarding(message.bot, message.chat.id, state, session, user, purpose)


@router.message(F.contact)
async def on_contact(
    message: Message, state: FSMContext, session: AsyncSession, user: User, settings: Settings
) -> None:
    if not user.onboarding_completed:
        await message.answer(texts.NEED_START, reply_markup=ReplyKeyboardRemove())
        return
    if not svc.is_own_contact(message.contact.user_id, message.from_user.id):
        await message.answer(texts.WRONG_CONTACT)
        return
    result = await svc.attach_phone(session, user, message.contact.phone_number, settings.phone_hash_pepper)
    await state.clear()
    granted = result == svc.PhoneResult.GRANTED and trial_available(user)
    await message.answer(
        texts.TRIAL_GRANTED if granted else texts.TRIAL_DENIED, reply_markup=ReplyKeyboardRemove()
    )
    await send_menu(message, user, settings)


@router.message(Onboarding.contact, F.text == texts.BTN_LATER)
async def on_contact_later(message: Message, state: FSMContext, user: User, settings: Settings) -> None:
    await state.clear()
    await message.answer(texts.TRIAL_SKIPPED, reply_markup=ReplyKeyboardRemove())
    await send_menu(message, user, settings)


@router.callback_query(F.data == kb.CB_TRIAL)
async def trial_start(callback: CallbackQuery, state: FSMContext, user: User) -> None:
    await callback.answer()
    if not svc.trial_offered(user):
        return
    await state.set_state(Onboarding.contact)
    await _say(callback.bot, callback.from_user.id, texts.ASK_CONTACT, reply_markup=kb.contact_kb())
