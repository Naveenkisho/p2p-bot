from aiogram.fsm.state import State, StatesGroup


class SellFlow(StatesGroup):
    network = State()        # picking TRC20 / BEP20 (only when BEP20 is enabled)
    service = State()        # picking the payout method (UPI/IMPS/…) — state-gated so
                             # a stale service button from before /start stops working
    amount = State()
    choose_bank = State()    # picking a saved bank right after the amount
    bank_details = State()   # typing a new bank to continue the order


class BankForOrder(StatesGroup):
    details = State()        # typing a new bank after the deposit is confirmed


class AddBank(StatesGroup):
    details = State()        # adding a bank from the My Bank Cards menu


class RefundFlow(StatesGroup):
    txid = State()           # collecting the deposit TXID for a refund request


class ClaimFlow(StatesGroup):
    txid = State()           # collecting the TXID to claim a missed/late payment
    pick = State()           # cold-pasted TXID: picking WHICH order it pays for
