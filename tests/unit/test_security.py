import time

import pytest

from app.core.errors import Unauthorized
from app.core.security import TelegramUser, verify_init_data
from tests.helpers import sign_init_data

TOKEN = "123456:TEST-TOKEN"
USER = {"id": 42, "first_name": "Ali", "username": "ali"}


def test_valid_init_data() -> None:
    user = verify_init_data(sign_init_data(USER, TOKEN), TOKEN)
    assert user == TelegramUser(id=42, first_name="Ali", username="ali")


def test_username_is_optional() -> None:
    init = sign_init_data({"id": 7, "first_name": "Vali"}, TOKEN)
    assert verify_init_data(init, TOKEN).username is None


def test_wrong_bot_token_rejected() -> None:
    with pytest.raises(Unauthorized):
        verify_init_data(sign_init_data(USER, TOKEN), "999:OTHER")


def test_tampered_field_rejected() -> None:
    init = sign_init_data(USER, TOKEN).replace("Ali", "Eve")
    with pytest.raises(Unauthorized):
        verify_init_data(init, TOKEN)


@pytest.mark.parametrize("init_data", ["", "user=%7B%7D", "hash=abc", "auth_date=1&hash=zz"])
def test_malformed_rejected(init_data: str) -> None:
    with pytest.raises(Unauthorized):
        verify_init_data(init_data, TOKEN)


def test_expired_rejected() -> None:
    old = int(time.time()) - 86400 - 60
    with pytest.raises(Unauthorized):
        verify_init_data(sign_init_data(USER, TOKEN, auth_date=old), TOKEN)


def test_custom_max_age() -> None:
    init = sign_init_data(USER, TOKEN, auth_date=int(time.time()) - 120)
    with pytest.raises(Unauthorized):
        verify_init_data(init, TOKEN, max_age_sec=60)
    assert verify_init_data(init, TOKEN, max_age_sec=600).id == 42


def test_far_future_auth_date_rejected() -> None:
    with pytest.raises(Unauthorized):
        verify_init_data(sign_init_data(USER, TOKEN, auth_date=int(time.time()) + 3600), TOKEN)


def test_missing_user_field_rejected() -> None:
    import hashlib
    import hmac

    fields = {"auth_date": str(int(time.time()))}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(Unauthorized):
        verify_init_data(f"auth_date={fields['auth_date']}&hash={digest}", TOKEN)


def test_empty_bot_token_rejected() -> None:
    with pytest.raises(Unauthorized):
        verify_init_data(sign_init_data(USER, TOKEN), "")
