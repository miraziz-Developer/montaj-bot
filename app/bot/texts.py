"""All user-facing bot texts (Uzbek, Latin script). Handlers must not contain literal user text."""

WELCOME = (
    "Salom, {name}! 👋\n\n"
    "Men — AI video montajchi. Videongizni yuboring: men uni sahna-sahna ko‘rib chiqaman, "
    "montaj rejasini tuzaman, siz tasdiqlagach tayyor videoni yasab beraman.\n\n"
    "✂️ Ortiqcha pauza va xatolarni kesish\n"
    "💬 Chiroyli subtitrlar\n"
    "🎵 Musiqa va dinamik montaj\n"
    "⚡️ Kunlar emas, daqiqalarda tayyor\n\n"
    "Avval sizni biroz tanib olay."
)
DEFAULT_NAME = "do‘st"

ASK_NICHE = "Qaysi sohada blog yuritasiz yoki video nima uchun kerak?"
NICHE_LABELS = {
    "auto": "🚗 Avto savdo",
    "shop": "🛍 Do‘kon / mahsulot",
    "edu": "🎓 Ta’lim / kurs",
    "food": "🍽 Restoran / oziq-ovqat",
    "beauty": "💄 Go‘zallik / salon",
    "blog": "🎥 Shaxsiy blog / vlog",
    "other": "✍️ Boshqa",
}
ASK_NICHE_OTHER = "Sohangizni qisqa yozing (masalan: “ko‘chmas mulk”)."

ASK_PURPOSE = "Videolarni qayerga joylaysiz?"
PURPOSE_LABELS = {
    "reels": "📱 Instagram Reels / TikTok",
    "youtube": "▶️ YouTube",
    "telegram": "✈️ Telegram kanal",
    "ads": "📢 Reklama",
    "other": "✍️ Boshqa",
}
ASK_PURPOSE_OTHER = "Qayerga joylashingizni qisqa yozing."

ASK_CONTACT = (
    "Bepul sinov uchun telefon raqamingizni tasdiqlang. Raqam faqat bir kishi sinovdan bir marta "
    "foydalanishi uchun kerak va boshqa maqsadda ishlatilmaydi."
)
TRIAL_GRANTED = (
    "Tayyor! 🎁 Sizga 1 ta bepul sinov videosi (1 daqiqagacha) berildi. "
    "Quyidagi tugma orqali videoni yuklang."
)
TRIAL_DENIED = (
    "Bu raqam bilan bepul sinov avval ishlatilgan. Tariflardan birini tanlashingiz mumkin: /tariflar"
)
TRIAL_SKIPPED = (
    "Yaxshi. Bepul sinovni istalgan payt menyudagi «🎁 Bepul sinov» tugmasi orqali "
    "faollashtirishingiz mumkin."
)
WRONG_CONTACT = "Iltimos, o‘z raqamingizni ulashing."

MENU_TITLE = "Bosh menyu:"
NO_VIDEO_IN_CHAT = (
    "Videoni yuklash uchun «🎬 Video yuklash» tugmasini bosing — u katta fayllarni ham ishonchli yuklaydi."
)
BALANCE = "💰 Balansingiz: {units} birlik.\n1 birlik = 3 daqiqagacha video."
BALANCE_TRIAL_LINE = "\n🎁 Bepul sinov mavjud."
HELP = (
    "Men videoni tahlil qilib, montaj rejasini tuzaman va tayyor videoni yasayman.\n\n"
    "1️⃣ «Video yuklash» tugmasini bosing\n"
    "2️⃣ Formatni tanlang\n"
    "3️⃣ Men rejani yuboraman — tasdiqlang yoki o‘zgartirishni yozing\n"
    "4️⃣ Tayyor video shu yerga keladi\n\n"
    "Buyruqlar: /balance, /tariflar, /help"
)
COMING_SOON = "Tez orada."

# Buttons
BTN_START = "Boshlash"
BTN_SHARE_CONTACT = "📱 Raqamni ulashish"
BTN_LATER = "Keyinroq"
BTN_UPLOAD = "🎬 Video yuklash"
BTN_TARIFFS = "💳 Tariflar"
BTN_BALANCE = "📊 Balans"
BTN_VIDEOS = "📁 Videolarim"
BTN_TRIAL = "🎁 Bepul sinov"

# Extra texts (not in the prompt's fixed list)
HINT = "Buyruqni tushunmadim. Menyudan foydalaning:"
NEED_START = "Boshlash uchun /start buyrug‘ini yuboring."
CANCELED = "Bekor qilindi."
UPLOAD_NEEDS_HTTPS = "Video yuklash tugmasi hozircha mavjud emas (Mini App uchun HTTPS manzil sozlanmagan)."

# Worker progress / failure messages
PROGRESS_PREPARING = "⏳ Video tayyorlanmoqda…"
PROGRESS_ANALYZING = "🔍 Sahnalar tahlil qilinmoqda…"
PROGRESS_PLANNING = "🧠 Montaj rejasi tuzilmoqda…"
PROGRESS_REVISING = "✏️ Reja o‘zgartirilmoqda…"
PROGRESS_RENDERING = "🎬 Video montaj qilinmoqda…"
FREE_EDITS_EXHAUSTED = "Bepul tahrirlar tugadi va balansda birlik yo‘q."
REVISION_NOT_APPLIED = "Bu o‘zgarishni amalga oshira olmadim, rejani o‘zgartirmadim."
FAILED_DOWNLOAD = (
    "Videoni yuklab olishda xatolik yuz berdi. Birliklaringiz qaytarildi, iltimos qayta urinib ko‘ring."
)
FAILED_PROBE = "Video faylni o‘qib bo‘lmadi. Birliklaringiz qaytarildi. Boshqa formatda yuborib ko‘ring."
FAILED_STT = "Nutqni matnga aylantirishda xatolik yuz berdi. Birliklaringiz qaytarildi, qayta urinib ko‘ring."
FAILED_ANALYSIS = (
    "Videoni tahlil qilishda xatolik yuz berdi. Birliklaringiz qaytarildi, qayta urinib ko‘ring."
)
FAILED_PLANNING = (
    "Montaj rejasini tuzishda xatolik yuz berdi. Birliklaringiz qaytarildi, qayta urinib ko‘ring."
)
FAILED_RENDER = "Videoni tayyorlashda xatolik yuz berdi. Birliklaringiz qaytarildi, qayta urinib ko‘ring."
FAILED_DELIVERY = "Tayyor videoni yuborib bo‘lmadi. Birliklaringiz qaytarildi, qayta urinib ko‘ring."
FAILED_STUCK = (
    "Video juda uzoq qayta ishlandi va to‘xtatildi. Birliklaringiz qaytarildi, qayta urinib ko‘ring."
)
FAILED_INTERNAL = "Kutilmagan xatolik yuz berdi. Birliklaringiz qaytarildi, iltimos qayta urinib ko‘ring."

# Plan approval and delivery
BTN_APPROVE = "✅ Tasdiqlash"
BTN_REVISE = "✏️ O‘zgartirish"
BTN_CANCEL_JOB = "❌ Bekor qilish"
BTN_RECHECK = "🔁 Qayta ko‘rish"
PLAN_TITLE = "🎬 {title}"
PLAN_TIMELINE = "📋 Reja:"
PLAN_MORE_CLIPS = "… yana {n} ta qism"
PLAN_TOTAL = "⏱ Jami: {duration}"
PLAN_WATERMARK = "🎁 Bepul sinov: videoda suv belgisi bo‘ladi."
PLAN_CHANGES = "🔧 O‘zgarishlar:"
PLAN_UNSUPPORTED = "⚠️ Bu narsalarni qila olmadim:"
ASK_REVISION = "Nimani o‘zgartirishni xohlaysiz? Yozing (masalan: “boshini qisqartir”, “musiqani o‘chir”)."
REVISION_QUEUED = "⏳ O‘zgartirilmoqda…"
CB_STARTED = "Boshladim!"
JOB_WRONG_STATE = "Bu video hozir bu holatda emas."
JOB_CANCELED = "❌ Bekor qilindi."
JOB_STATUS_NOW = "Video holati: {status}"
DELIVERED_DONE = "✅ Tayyor!"
DELIVERY_CAPTION = "✅ Video tayyor!\n\nFayl 48 soatdan keyin o‘chiriladi."
DELIVERY_LINK = "✅ Video tayyor!\n\nYuklab olish (48 soat amal qiladi):\n{url}"
FAILED_RETRY_HINT = "/start orqali qayta urinib ko‘rishingiz mumkin."
ENQUEUE_FAILED = "Xatolik yuz berdi, iltimos qayta urinib ko‘ring."

# Payments (manual, MVP)
PAYMENT_INSTRUCTIONS = (
    "«{label}» tarifi — {units} birlik, {price} so‘m.\n\n"
    "Quyidagi kartaga to‘lovni amalga oshiring:\n{card}\n\n"
    "To‘lov chekini (skrinshot yoki rasm) shu yerga yuboring."
)
RECEIPT_RECEIVED = "Rahmat! Chekingiz tekshirilmoqda, tez orada javob beramiz."
PLEASE_SEND_PHOTO = "Iltimos, to‘lov chekining rasmini yuboring."
PAYMENT_APPROVED = (
    "✅ To‘lovingiz tasdiqlandi! Hisobingizga {units} birlik qo‘shildi. Joriy balans: {balance} birlik."
)
PAYMENT_REJECTED = "❌ To‘lovingiz tasdiqlanmadi. Savol bo‘lsa, admin bilan bog‘laning."
TARIFF_CARD = "{emoji} {label}\n{units} ta video (3 daqiqagacha)\n{price} so‘m{tag}"
TARIFF_TAG_RECOMMENDED = "\n⭐ Tavsiya etiladi"
TARIFFS_TITLE = "💳 Tariflar:"
MYVIDEOS_EMPTY = "Hali birorta video yubormagansiz."
MYVIDEOS_TITLE = "📁 So‘nggi videolaringiz:"
VIDEO_FALLBACK_TITLE = "Video #{short_id}"
CARD_NOT_CONFIGURED = "To‘lov rekvizitlari hozircha mavjud emas. Iltimos, admin bilan bog‘laning."
PAYMENT_STALE = "Bu to‘lov so‘rovi endi amal qilmaydi."
PAYMENT_CANCELED = "To‘lov so‘rovi bekor qilindi."
PAYMENT_ALREADY_DECIDED = "Bu to‘lov allaqachon ko‘rib chiqilgan."
BTN_PAY_OK = "✅ Tasdiqlash"
BTN_PAY_NO = "❌ Rad etish"
ADMIN_RECEIPT_CAPTION = (
    "💳 To‘lov cheki\n"
    "Foydalanuvchi: {name} (id {telegram_id}{username})\n"
    "Tarif: {label} — {units} birlik\n"
    "Summa: {price} so‘m\n"
    "So‘rov: {short_id}"
)
ADMIN_DECISION_APPROVED = "✅ Tasdiqlandi — {admin}"
ADMIN_DECISION_REJECTED = "❌ Rad etildi — {admin}"
TARIFF_EMOJI = {"single": "🎬", "start": "🚀", "pro": "💎", "max": "👑"}
JOB_STATUS_UZ = {
    "CREATED": "🆕 Yaratildi",
    "AWAITING_CONFIRM": "🕓 Boshlash kutilmoqda",
    "QUEUED": "🕓 Navbatda",
    "PREPROCESSING": "🔍 Tayyorlanmoqda",
    "ANALYZING": "🔍 Tahlil qilinmoqda",
    "PLANNING": "🧠 Reja tuzilmoqda",
    "AWAITING_PLAN_APPROVAL": "⏳ Tasdiq kutilmoqda",
    "REVISING": "✏️ O‘zgartirilmoqda",
    "RENDERING": "🎬 Montaj qilinmoqda",
    "DELIVERING": "📤 Yuborilmoqda",
    "DONE": "✅ Tayyor",
    "FAILED": "❌ Xatolik",
    "CANCELED": "🚫 Bekor qilingan",
    "EXPIRED": "⌛️ Muddati o‘tgan",
}
AGO_NOW = "hozir"
AGO_MINUTES = "{n} daqiqa oldin"
AGO_HOURS = "{n} soat oldin"
AGO_YESTERDAY = "kecha"
AGO_DAYS = "{n} kun oldin"

# Admin
GRANT_USAGE = "Foydalanish: /grant <telegram_id> <birlik>"
USER_NOT_FOUND = "Foydalanuvchi topilmadi."
GRANT_DONE = "✅ {telegram_id} hisobiga {units} birlik qo‘shildi. Yangi balans: {balance}"
GRANTED_NOTIFY = "🎁 Hisobingizga {n} birlik qo‘shildi."
STATS = (
    "📊 Statistika\n"
    "Foydalanuvchilar: {total}\n"
    "Ro‘yxatdan o‘tganlar: {onboarded}\n"
    "Berilgan birliklar: {granted}\n"
    "Videolar:\n{jobs}"
)
STATS_NO_JOBS = "—"

COMMANDS = [
    ("start", "Boshlash"),
    ("help", "Yordam"),
    ("balance", "Balans"),
    ("tariflar", "Tariflar"),
    ("myvideos", "Videolarim"),
    ("cancel", "Bekor qilish"),
]
