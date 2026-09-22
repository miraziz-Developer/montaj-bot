"""/tariflar, manual payment (receipt photo + admin approval) and /myvideos."""

import logging
import uuid
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.states import Billing
from app.core.config import Settings
from app.core.errors import InvalidState, NotFound
from app.models.enums import PaymentStatus
from app.models.job import Job
from app.models.payment import Payment
from app.models.plan import EditPlanRow
from app.models.user import User
from app.services import payments
from app.services.tariffs import TARIFFS, get_tariff

logger = logging.getLogger(__name__)
router = Router(name="billing")
router.message.filter(F.chat.type == "private")

MYVIDEOS_LIMIT = 10
TITLE_MAX_CHARS = 40


# ---------- formatting helpers ----------


def fmt_uzs(amount: int) -> str:
    return f"{amount:,}".replace(",", " ")


def relative_uz(moment: datetime, now: datetime | None = None) -> str:
    seconds = ((now or datetime.now(UTC)) - moment).total_seconds()
    if seconds < 60:
        return texts.AGO_NOW
    if seconds < 3600:
        return texts.AGO_MINUTES.format(n=int(seconds // 60))
    if seconds < 86400:
        return texts.AGO_HOURS.format(n=int(seconds // 3600))
    if seconds < 172800:
        return texts.AGO_YESTERDAY
    return texts.AGO_DAYS.format(n=int(seconds // 86400))


def tariffs_text() -> str:
    cards = [
        texts.TARIFF_CARD.format(
            emoji=texts.TARIFF_EMOJI.get(t.code, "•"),
            label=t.label,
            units=t.units,
            price=fmt_uzs(t.price_uzs),
            tag=texts.TARIFF_TAG_RECOMMENDED if t.recommended else "",
        )
        for t in TARIFFS
    ]
    return texts.TARIFFS_TITLE + "\n\n" + "\n\n".join(cards)


def _parse_id(data: str | None, prefix: str = "") -> uuid.UUID | None:
    try:
        return uuid.UUID((data or "").removeprefix(prefix))
    except ValueError:
        return None


# ---------- tariffs ----------


@router.message(Command("tariflar"))
async def cmd_tariffs(message: Message) -> None:
    await message.answer(tariffs_text(), reply_markup=kb.tariffs_kb(TARIFFS, fmt_uzs))


@router.callback_query(F.data == kb.CB_TARIFFS)
async def cb_tariffs(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.bot.send_message(
        callback.from_user.id, tariffs_text(), reply_markup=kb.tariffs_kb(TARIFFS, fmt_uzs)
    )


@router.callback_query(F.data.startswith(kb.CB_BUY))
async def buy(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User, settings: Settings
) -> None:
    await callback.answer()
    try:
        tariff = get_tariff(callback.data.removeprefix(kb.CB_BUY))
    except NotFound:
        return
    if not settings.payment_card_text.strip():
        await callback.bot.send_message(callback.from_user.id, texts.CARD_NOT_CONFIGURED)
        return
    payment = await payments.create_payment_request(session, user, tariff.code)
    await state.set_state(Billing.waiting_receipt)
    await state.update_data(payment_id=str(payment.id))
    await callback.bot.send_message(
        callback.from_user.id,
        texts.PAYMENT_INSTRUCTIONS.format(
            label=tariff.label,
            units=tariff.units,
            price=fmt_uzs(payment.amount_uzs),
            card=settings.payment_card_text,
        ),
        reply_markup=kb.payment_cancel_kb(payment.id),
    )


# ---------- receipt ----------


async def _owned_pending(session: AsyncSession, payment_id: uuid.UUID | None, user: User) -> Payment | None:
    payment = await session.get(Payment, payment_id) if payment_id else None
    if payment is None or payment.user_id != user.id or payment.status != PaymentStatus.PENDING:
        return None
    return payment


async def _notify_admins(bot: Bot, settings: Settings, payment: Payment, user: User, file_id: str) -> None:
    tariff = get_tariff(payment.plan_code)
    caption = texts.ADMIN_RECEIPT_CAPTION.format(
        name=user.first_name or "—",
        telegram_id=user.telegram_id,
        username=f", @{user.username}" if user.username else "",
        label=tariff.label,
        units=tariff.units,
        price=fmt_uzs(payment.amount_uzs),
        short_id=str(payment.id)[:8],
    )
    if not settings.admin_telegram_ids:
        logger.warning(
            "no ADMIN_TELEGRAM_IDS configured: receipt payment_id=%s cannot be reviewed", payment.id
        )
    for admin_id in settings.admin_telegram_ids:
        try:  # re-uses the file_id: no download, no upload
            await bot.send_photo(
                admin_id, photo=file_id, caption=caption, reply_markup=kb.payment_decision_kb(payment.id)
            )
        except TelegramAPIError:
            logger.warning("could not send the receipt to admin telegram_id=%s", admin_id, exc_info=True)


@router.message(Billing.waiting_receipt, F.photo)
async def receipt_photo(
    message: Message, state: FSMContext, session: AsyncSession, user: User, settings: Settings
) -> None:
    payment = await _owned_pending(session, _parse_id((await state.get_data()).get("payment_id")), user)
    await state.clear()
    if payment is None:
        await message.answer(texts.PAYMENT_STALE)
        return
    file_id = message.photo[-1].file_id  # the largest size
    await payments.attach_receipt(session, payment, file_id)
    await session.commit()
    await message.answer(texts.RECEIPT_RECEIVED)
    await _notify_admins(message.bot, settings, payment, user, file_id)


@router.message(Billing.waiting_receipt, ~F.text.startswith("/"))
async def receipt_not_a_photo(message: Message) -> None:
    await message.answer(texts.PLEASE_SEND_PHOTO)  # stays in the same state


@router.callback_query(F.data.startswith(kb.CB_PAY_CANCEL))
async def pay_cancel(callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
    payment_id = _parse_id(callback.data, kb.CB_PAY_CANCEL)
    payment = await session.get(Payment, payment_id) if payment_id else None
    if payment is None or payment.user_id != user.id:
        await callback.answer()  # someone else's payment: silent
        return
    try:
        await payments.cancel_payment(session, payment, user)
    except InvalidState:
        await callback.answer(texts.PAYMENT_STALE)
        return
    if (await state.get_data()).get("payment_id") == str(payment.id):
        await state.clear()
    await callback.answer()
    try:
        await callback.message.edit_text(texts.PAYMENT_CANCELED, reply_markup=None)  # type: ignore[union-attr]
    except (TelegramAPIError, AttributeError):
        await callback.bot.send_message(callback.from_user.id, texts.PAYMENT_CANCELED)


# ---------- admin decision ----------


async def _mark_decision(callback: CallbackQuery, decision: str) -> None:
    """Show the decision (and who made it) on the admin's receipt message; buttons disappear."""
    message = callback.message
    try:
        await message.edit_caption(
            caption=f"{message.caption or ''}\n\n{decision}".strip(), reply_markup=None
        )  # type: ignore[union-attr]
    except (TelegramAPIError, AttributeError):
        try:
            await message.edit_text(decision, reply_markup=None)  # type: ignore[union-attr]
        except (TelegramAPIError, AttributeError):
            logger.warning("could not mark the decision on the admin message", exc_info=True)


@router.callback_query(F.data.startswith((kb.CB_PAY_OK, kb.CB_PAY_NO)))
async def pay_decide(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    if not user.is_admin:  # a guessed callback: silent for the user, loud in the logs
        logger.warning("non-admin telegram_id=%s tried a payment decision", callback.from_user.id)
        await callback.answer()
        return
    approve = callback.data.startswith(kb.CB_PAY_OK)
    payment_id = _parse_id(callback.data, kb.CB_PAY_OK if approve else kb.CB_PAY_NO)
    payment = await session.get(Payment, payment_id) if payment_id else None
    if payment is None:
        await callback.answer()
        return
    try:
        if approve:
            payment = await payments.approve_payment(session, payment, user)
        else:
            payment = await payments.reject_payment(session, payment, user)
    except InvalidState:  # already decided (double click, or another admin was faster)
        await callback.answer(texts.PAYMENT_ALREADY_DECIDED, show_alert=True)
        return
    await session.commit()

    payer = await session.get(User, payment.user_id, populate_existing=True)
    await callback.answer()
    admin_name = user.first_name or str(user.telegram_id)
    template = texts.ADMIN_DECISION_APPROVED if approve else texts.ADMIN_DECISION_REJECTED
    await _mark_decision(callback, template.format(admin=admin_name))
    text = (
        texts.PAYMENT_APPROVED.format(units=get_tariff(payment.plan_code).units, balance=payer.balance_units)
        if approve
        else texts.PAYMENT_REJECTED
    )
    try:
        await callback.bot.send_message(payer.telegram_id, text)
    except TelegramAPIError:
        logger.warning("could not notify the payer telegram_id=%s", payer.telegram_id, exc_info=True)


# ---------- my videos ----------


async def videos_text(session: AsyncSession, user: User, now: datetime | None = None) -> str:
    title = EditPlanRow.plan_json["title"].astext
    rows = (
        await session.execute(
            select(Job, title)
            .outerjoin(
                EditPlanRow,
                and_(EditPlanRow.job_id == Job.id, EditPlanRow.version == Job.current_plan_version),
            )
            .where(Job.user_id == user.id)
            .order_by(Job.created_at.desc())
            .limit(MYVIDEOS_LIMIT)
        )
    ).all()
    if not rows:
        return texts.MYVIDEOS_EMPTY
    lines = []
    for index, (job, plan_title) in enumerate(rows, start=1):
        name = (plan_title or "").strip()[:TITLE_MAX_CHARS] or texts.VIDEO_FALLBACK_TITLE.format(
            short_id=str(job.id)[:6]
        )
        status = texts.JOB_STATUS_UZ.get(job.status.value, job.status.value)
        lines.append(f"{index}. {status} · {relative_uz(job.created_at, now)}\n    {name}")
    return texts.MYVIDEOS_TITLE + "\n\n" + "\n".join(lines)


@router.message(Command("myvideos"))
async def cmd_myvideos(message: Message, session: AsyncSession, user: User) -> None:
    await message.answer(await videos_text(session, user))


@router.callback_query(F.data == kb.CB_VIDEOS)
async def cb_myvideos(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await callback.answer()
    await callback.bot.send_message(callback.from_user.id, await videos_text(session, user))
