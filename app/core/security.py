"""Telegram Mini App initData verification (HMAC-SHA256), see core.telegram.org/bots/webapps."""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from app.core.errors import Unauthorized

_CLOCK_SKEW_SEC = 300


@dataclass(frozen=True, slots=True)
class TelegramUser:
    id: int
    first_name: str
    username: str | None


def verify_init_data(init_data: str, bot_token: str, max_age_sec: int = 86400) -> TelegramUser:
    """Verify the signature and freshness of `init_data`; raise Unauthorized on ANY failure."""
    if not init_data or not bot_token:
        raise Unauthorized()
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = fields.pop("hash", None)
    if not received_hash:
        raise Unauthorized()

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise Unauthorized()

    try:
        age = time.time() - int(fields["auth_date"])
        user = json.loads(fields["user"])
        telegram_user = TelegramUser(
            id=int(user["id"]),
            first_name=str(user.get("first_name", "")),
            username=user.get("username"),
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        raise Unauthorized() from None
    if age > max_age_sec or age < -_CLOCK_SKEW_SEC:
        raise Unauthorized()
    return telegram_user
