from aiogram.fsm.state import State, StatesGroup


class Onboarding(StatesGroup):
    niche_text = State()
    purpose_text = State()
    contact = State()


class Revision(StatesGroup):
    waiting_text = State()


class Billing(StatesGroup):
    waiting_receipt = State()
