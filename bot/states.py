from aiogram.fsm.state import State, StatesGroup


class SellFlow(StatesGroup):
    amount = State()
    bank_details = State()   # typing in a new bank while ordering
    choose_bank = State()


class AddBank(StatesGroup):
    details = State()        # adding a bank from the My Bank Cards menu


class RefundFlow(StatesGroup):
    address = State()        # collecting the TRC20 refund address after a cancel
