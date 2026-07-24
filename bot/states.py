from aiogram.fsm.state import State, StatesGroup


class SellFlow(StatesGroup):
    amount = State()
    choose_bank = State()    # picking a saved bank right after the amount
    bank_details = State()   # typing a new bank to continue the order


class BankForOrder(StatesGroup):
    details = State()        # typing a new bank after the deposit is confirmed


class AddBank(StatesGroup):
    details = State()        # adding a bank from the My Bank Cards menu


class RefundFlow(StatesGroup):
    address = State()        # collecting the TRC20 refund address after a cancel
