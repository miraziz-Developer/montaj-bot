from fastapi import APIRouter, Depends

from app.api.deps import current_user
from app.models.user import User
from app.schemas.api import MeOut, TariffOut
from app.services.billing import trial_available
from app.services.tariffs import TARIFFS

router = APIRouter(prefix="/api", tags=["me"])


@router.get("/me", response_model=MeOut)
async def me(user: User = Depends(current_user)) -> MeOut:
    return MeOut(
        telegram_id=user.telegram_id,
        first_name=user.first_name,
        balance_units=user.balance_units,
        trial_available=trial_available(user),
        onboarding_completed=user.onboarding_completed,
        tariffs=[
            TariffOut(
                code=t.code, units=t.units, price_uzs=t.price_uzs, label=t.label, recommended=t.recommended
            )
            for t in TARIFFS
        ],
    )
