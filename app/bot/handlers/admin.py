import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.core.errors import NotFound
from app.models.user import User
from app.services import onboarding as svc

logger = logging.getLogger(__name__)
router = Router(name="admin")
router.message.filter(F.chat.type == "private")


@router.message(Command("grant"))
async def cmd_grant(message: Message, session: AsyncSession, user: User) -> None:
    if not user.is_admin:  # non-admins get no reply at all
        return
    try:
        telegram_id, units = svc.parse_grant_args(message.text or "")
    except ValueError:
        await message.answer(texts.GRANT_USAGE)
        return
    try:
        target, balance = await svc.grant_by_telegram_id(session, user, telegram_id, units)
    except NotFound:
        await message.answer(texts.USER_NOT_FOUND)
        return
    await message.answer(texts.GRANT_DONE.format(telegram_id=telegram_id, units=units, balance=balance))
    try:
        await message.bot.send_message(target.telegram_id, texts.GRANTED_NOTIFY.format(n=units))
    except Exception:
        logger.warning("could not notify granted user telegram_id=%s", telegram_id)
    logger.info("admin grant admin=%s target=%s units=%s", user.telegram_id, telegram_id, units)


@router.message(Command("stats"))
async def cmd_stats(message: Message, session: AsyncSession, user: User) -> None:
    if not user.is_admin:
        return
    stats = await svc.collect_stats(session)
    jobs = "\n".join(f"  {status}: {count}" for status, count in sorted(stats.jobs_by_status.items()))
    await message.answer(
        texts.STATS.format(
            total=stats.total_users,
            onboarded=stats.onboarded_users,
            granted=stats.units_granted,
            jobs=jobs or texts.STATS_NO_JOBS,
        )
    )
