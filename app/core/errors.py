from typing import Any


class DomainError(Exception):
    """Raised by services; converted to HTTP/Telegram messages only at the edges."""

    code = "DOMAIN_ERROR"
    message_uz = "Xatolik yuz berdi."
    http_status = 400

    def __init__(
        self,
        code: str | None = None,
        message_uz: str | None = None,
        http_status: int | None = None,
    ) -> None:
        self.code = code or type(self).code
        self.message_uz = message_uz or type(self).message_uz
        self.http_status = http_status or type(self).http_status
        super().__init__(f"{self.code}: {self.message_uz}")

    def to_body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message_uz": self.message_uz}}


class InsufficientUnits(DomainError):
    code = "INSUFFICIENT_UNITS"
    message_uz = "Birliklar yetarli emas."
    http_status = 402


class InvalidState(DomainError):
    code = "INVALID_STATE"
    message_uz = "Amal hozir mumkin emas."
    http_status = 409


class NotFound(DomainError):
    code = "NOT_FOUND"
    message_uz = "Topilmadi."
    http_status = 404


class Forbidden(DomainError):
    code = "FORBIDDEN"
    message_uz = "Ruxsat yo‘q."
    http_status = 403


class TooManyActiveJobs(DomainError):
    code = "TOO_MANY_ACTIVE_JOBS"
    message_uz = "Faol videolar soni chegaradan oshdi."
    http_status = 429


class TrialUnavailable(DomainError):
    code = "TRIAL_UNAVAILABLE"
    message_uz = "Bepul sinov mavjud emas."
    http_status = 409


class Unauthorized(DomainError):
    code = "UNAUTHORIZED"
    message_uz = "Avtorizatsiya talab qilinadi."
    http_status = 401


class InvalidMedia(DomainError):
    code = "INVALID_MEDIA"
    message_uz = "Video fayl yaroqsiz."
    http_status = 422


class FileTooLarge(DomainError):
    code = "FILE_TOO_LARGE"
    message_uz = "Fayl hajmi juda katta."
    http_status = 413


class UnsupportedType(DomainError):
    code = "UNSUPPORTED_TYPE"
    message_uz = "Bu fayl turi qo‘llab-quvvatlanmaydi."
    http_status = 415


class NoCredits(DomainError):
    code = "NO_CREDITS"
    message_uz = "Video yuklash uchun birlik yoki bepul sinov kerak."
    http_status = 402


class OnboardingRequired(DomainError):
    code = "ONBOARDING_REQUIRED"
    message_uz = "Avval botda ro‘yxatdan o‘ting."
    http_status = 403


class BlobMissing(DomainError):
    code = "BLOB_MISSING"
    message_uz = "Fayl topilmadi. Qayta yuklang."
    http_status = 400


class SizeMismatch(DomainError):
    code = "SIZE_MISMATCH"
    message_uz = "Fayl hajmi mos kelmadi. Qayta yuklang."
    http_status = 422


class TooLong(DomainError):
    code = "TOO_LONG"
    message_uz = "Video juda uzun."
    http_status = 422


class AIError(DomainError):
    """The LLM failed or kept returning invalid JSON. `usage` = tokens already spent (cost tracking)."""

    code = "AI_ERROR"
    message_uz = "AI xizmati vaqtincha ishlamayapti. Keyinroq urinib ko‘ring."
    http_status = 502

    def __init__(self, detail: str = "", usage: Any = None) -> None:
        super().__init__()
        self.detail = detail
        self.usage = usage
        if detail:
            self.args = (f"{self.code}: {detail}",)


class RenderError(DomainError):
    code = "RENDER_FAILED"
    message_uz = "Videoni tayyorlashda xatolik yuz berdi."
    http_status = 500


class TrialTooLong(DomainError):
    code = "TRIAL_TOO_LONG"
    http_status = 422

    def __init__(self, limit_sec: int) -> None:
        super().__init__(message_uz=f"Bepul sinov uchun video {limit_sec} soniyadan oshmasligi kerak.")
