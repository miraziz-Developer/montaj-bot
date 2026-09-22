from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from app.bot import texts
from app.core.config import Settings

CB_ONB_START = "onb:start"
CB_NICHE = "onb:niche:"
CB_PURPOSE = "onb:purpose:"
CB_TRIAL = "trial:start"
CB_TARIFFS = "menu:tariffs"
CB_BALANCE = "menu:balance"
CB_VIDEOS = "menu:videos"
CB_PLAN_APPROVE = "plan:approve:"
CB_PLAN_REVISE = "plan:revise:"
CB_PLAN_CANCEL = "plan:cancel:"
CB_PLAN_SHOW = "plan:show:"
CB_BUY = "buy:"
CB_PAY_OK = "pay:ok:"
CB_PAY_NO = "pay:no:"
CB_PAY_CANCEL = "pay:cancel:"


def mini_app_url(settings: Settings) -> str | None:
    """The Mini App URL, or None when PUBLIC_BASE_URL is not https (Telegram rejects http web_app buttons)."""
    url = settings.public_base_url.rstrip("/") + "/app/"
    return url if url.startswith("https://") else None


def welcome_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=texts.BTN_START, callback_data=CB_ONB_START)]]
    )


def _choice_kb(prefix: str, labels: dict[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=prefix + key)] for key, label in labels.items()
        ]
    )


def niche_kb() -> InlineKeyboardMarkup:
    return _choice_kb(CB_NICHE, texts.NICHE_LABELS)


def purpose_kb() -> InlineKeyboardMarkup:
    return _choice_kb(CB_PURPOSE, texts.PURPOSE_LABELS)


def contact_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.BTN_SHARE_CONTACT, request_contact=True)],
            [KeyboardButton(text=texts.BTN_LATER)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def main_menu(web_app_url: str | None, trial_offer: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if web_app_url:
        rows.append([InlineKeyboardButton(text=texts.BTN_UPLOAD, web_app=WebAppInfo(url=web_app_url))])
    if trial_offer:
        rows.append([InlineKeyboardButton(text=texts.BTN_TRIAL, callback_data=CB_TRIAL)])
    rows.append(
        [
            InlineKeyboardButton(text=texts.BTN_TARIFFS, callback_data=CB_TARIFFS),
            InlineKeyboardButton(text=texts.BTN_BALANCE, callback_data=CB_BALANCE),
        ]
    )
    rows.append([InlineKeyboardButton(text=texts.BTN_VIDEOS, callback_data=CB_VIDEOS)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plan_kb(job_id: object) -> InlineKeyboardMarkup:
    """The three buttons under a plan. Callback data `plan:<action>:<uuid>` is at most 49 bytes."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.BTN_APPROVE, callback_data=f"{CB_PLAN_APPROVE}{job_id}")],
            [
                InlineKeyboardButton(text=texts.BTN_REVISE, callback_data=f"{CB_PLAN_REVISE}{job_id}"),
                InlineKeyboardButton(text=texts.BTN_CANCEL_JOB, callback_data=f"{CB_PLAN_CANCEL}{job_id}"),
            ],
        ]
    )


def recheck_kb(job_id: object) -> InlineKeyboardMarkup:
    """Shown when a button refers to a job that is no longer in the expected state."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.BTN_RECHECK, callback_data=f"{CB_PLAN_SHOW}{job_id}")]
        ]
    )


def _tariff_button_text(tariff, fmt_price) -> str:  # noqa: ANN001
    emoji = texts.TARIFF_EMOJI.get(tariff.code, "")
    return f"{emoji} {tariff.label} — {fmt_price(tariff.price_uzs)} so‘m".strip()


def tariffs_kb(tariffs: list, fmt_price) -> InlineKeyboardMarkup:  # noqa: ANN001
    """One button per tariff: `buy:<plan_code>`."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_tariff_button_text(t, fmt_price),
                    callback_data=f"{CB_BUY}{t.code}",
                )
            ]
            for t in tariffs
        ]
    )


def payment_cancel_kb(payment_id: object) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.BTN_CANCEL_JOB, callback_data=f"{CB_PAY_CANCEL}{payment_id}")]
        ]
    )


def payment_decision_kb(payment_id: object) -> InlineKeyboardMarkup:
    """The two admin buttons under a receipt photo."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.BTN_PAY_OK, callback_data=f"{CB_PAY_OK}{payment_id}"),
                InlineKeyboardButton(text=texts.BTN_PAY_NO, callback_data=f"{CB_PAY_NO}{payment_id}"),
            ]
        ]
    )
