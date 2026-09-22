from app.bot import keyboards as kb
from app.bot import texts
from app.core.config import Settings


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def test_mini_app_url_requires_https() -> None:
    https = Settings(_env_file=None, public_base_url="https://montaj.example.com/")
    assert kb.mini_app_url(https) == "https://montaj.example.com/app/"
    assert kb.mini_app_url(Settings(_env_file=None, public_base_url="http://localhost:8000")) is None


def test_niche_and_purpose_keyboards_callback_data() -> None:
    assert _callbacks(kb.niche_kb()) == [
        "onb:niche:auto",
        "onb:niche:shop",
        "onb:niche:edu",
        "onb:niche:food",
        "onb:niche:beauty",
        "onb:niche:blog",
        "onb:niche:other",
    ]
    assert _callbacks(kb.purpose_kb()) == [
        "onb:purpose:reels",
        "onb:purpose:youtube",
        "onb:purpose:telegram",
        "onb:purpose:ads",
        "onb:purpose:other",
    ]


def test_callback_data_fits_telegram_limit() -> None:
    for markup in (kb.niche_kb(), kb.purpose_kb(), kb.welcome_kb(), kb.main_menu("https://x/app/", True)):
        assert all(len(data.encode()) <= 64 for data in _callbacks(markup))


def test_main_menu_with_web_app_and_trial() -> None:
    menu = kb.main_menu("https://montaj.example.com/app/", trial_offer=True)
    rows = menu.inline_keyboard
    assert rows[0][0].text == "🎬 Video yuklash"
    assert rows[0][0].web_app.url == "https://montaj.example.com/app/"
    assert _callbacks(menu) == ["trial:start", "menu:tariffs", "menu:balance", "menu:videos"]


def test_main_menu_without_web_app_or_trial() -> None:
    menu = kb.main_menu(None, trial_offer=False)
    assert all(b.web_app is None for row in menu.inline_keyboard for b in row)
    assert _callbacks(menu) == ["menu:tariffs", "menu:balance", "menu:videos"]


def test_contact_keyboard_requests_contact() -> None:
    markup = kb.contact_kb()
    assert markup.keyboard[0][0].request_contact is True
    assert markup.keyboard[0][0].text == "📱 Raqamni ulashish"
    assert markup.keyboard[1][0].text == "Keyinroq"


def test_texts_match_the_spec() -> None:
    assert texts.WELCOME.startswith("Salom, {name}! 👋\n\nMen — AI video montajchi.")
    assert texts.ASK_NICHE == "Qaysi sohada blog yuritasiz yoki video nima uchun kerak?"
    assert texts.NICHE_LABELS["edu"] == "🎓 Ta’lim / kurs"
    assert texts.PURPOSE_LABELS["reels"] == "📱 Instagram Reels / TikTok"
    assert texts.BALANCE.format(units=3) == "💰 Balansingiz: 3 birlik.\n1 birlik = 3 daqiqagacha video."
    assert texts.WRONG_CONTACT == "Iltimos, o‘z raqamingizni ulashing."
    assert texts.COMING_SOON == "Tez orada."
    assert texts.TRIAL_DENIED.endswith("/tariflar")
