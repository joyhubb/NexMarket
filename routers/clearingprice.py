from fastapi import APIRouter
from typing import List, Optional, Dict
from pydantic import BaseModel, Field
from datetime import datetime
from collections import defaultdict
import copy

router = APIRouter()

# =========================
# MODELS
# =========================

class UserSettlement(BaseModel):
    user_id: str
    spent: int
    refund: int

class Order(BaseModel):
    id: str
    user_id: str
    price: int = Field(ge=1, le=99)
    remain: int = Field(gt=0)
    created_at: datetime

class Trade(BaseModel):
    buy_yes_user_id: str
    buy_no_user_id: str
    yes_order_id: str
    no_order_id: str
    price: int
    quantity: int 

class ATORequest(BaseModel):
    option_id: str
    orders_yes: List[Order]
    orders_no: List[Order]

class ATOResponse(BaseModel):
    clearing_price_yes: Optional[int]
    clearing_price_no: Optional[int]
    matched_volume: int
    trades: List[Trade]
    settlements: list[UserSettlement]

# =========================
# CLEARING PRICE
# =========================

def calculate_clearing_price(orders_yes: List[Order], orders_no: List[Order]):
    yes_volume = [0] * 101
    no_volume = [0] * 101

    for o in orders_yes:
        yes_volume[o.price] += o.remain

    for o in orders_no:
        no_volume[o.price] += o.remain
    
    demand_yes = [0] * 101
    demand_no = [0] * 101
 
    running = 0
    for p in range(99, 0, -1):
        running += yes_volume[p]
        demand_yes[p] = running

    running = 0
    for p in range(99, 0, -1):
        running += no_volume[p]
        demand_no[p] = running
    
    best_p = None
    best_volume = 0

    for p in range(99, 0, -1):
        demand_y = demand_yes[p]
        demand_n = demand_no[100 - p]
        matched = min(demand_y, demand_n)

        if matched > best_volume:
            best_volume = matched
            best_p = p

    if best_volume == 0:
        return None, 0

    return best_p, best_volume

# =========================
# FILTER + SORT
# =========================

def filter_orders(orders_yes: List[Order], orders_no: List[Order], p: int):
    yes_valid = [o for o in orders_yes if o.price >= p]
    no_valid = [o for o in orders_no if o.price >= (100 - p)]
    return yes_valid, no_valid

def sort_orders(yes_orders: List[Order], no_orders: List[Order]):
    yes_orders.sort(key=lambda o: (-o.price, o.created_at))
    no_orders.sort(key=lambda o: (-o.price, o.created_at))

# =========================
# MATCHING ENGINE
# =========================

def match_order(yes_orders: List[Order], no_orders: List[Order], p: int, max_volume: int):
    trades = []
    i, j = 0, 0
    remaining = max_volume
    while (
        remaining > 0
        and i < len(yes_orders)
        and j < len(no_orders)
    ):
        y = yes_orders[i]
        n = no_orders[j]

        qty = min(y.remain, n.remain, remaining)

        trades.append(Trade(
            buy_yes_user_id=y.user_id,
            buy_no_user_id=n.user_id,
            yes_order_id=y.id,
            no_order_id=n.id,
            price=p,
            quantity=qty
        ))

        y.remain -= qty
        n.remain -= qty
        remaining -= qty
        if y.remain == 0:
            i += 1
        if n.remain == 0:
            j += 1
    return trades

# =========================
# SETTLEMENT ENGINE
# =========================

def calculate_settlements(trades: List[Trade], yes_map: Dict[str, Order], no_map: Dict[str, Order], p: int) -> List[UserSettlement]:
    settlements = defaultdict(lambda: {"spent" : 0, "refund" : 0})
    for t in trades:
        y_order = yes_map[t.yes_order_id]
        n_order = no_map[t.no_order_id]
        qty = t.quantity

        locked_yes = y_order.price * qty
        actual_yes = p * qty
        refund_yes = locked_yes - actual_yes

        settlements[t.buy_yes_user_id]["spent"] += actual_yes
        settlements[t.buy_yes_user_id]["refund"] += refund_yes

        locked_no = n_order.price * qty
        actual_no = (100 - p) * qty
        refund_no = locked_no - actual_no

        settlements[t.buy_no_user_id]["spent"] += actual_no
        settlements[t.buy_no_user_id]["refund"] += refund_no

    return [
        UserSettlement(
            user_id = uid,
            spent = data["spent"],
            refund = data["refund"]
        )
        for uid, data in settlements.items()
    ]

# =========================
# API
# =========================
@router.post("/clear", response_model=ATOResponse)
def clear_ato(data: ATORequest):

    yes_map = {o.id: copy.deepcopy(o) for o in data.orders_yes}
    no_map = {o.id: copy.deepcopy(o) for o in data.orders_no}
    
    yes_orders = copy.deepcopy(data.orders_yes)
    no_orders = copy.deepcopy(data.orders_no)

    p, matched_volume = calculate_clearing_price(yes_orders, no_orders)

    if p is None:
        return {
            "clearing_price_yes": None,
            "clearing_price_no": None,
            "matched_volume": 0,
            "trades": [],
            "settlements": []
        }

    yes_valid, no_valid = filter_orders(yes_orders, no_orders, p)

    sort_orders(yes_valid, no_valid)

    trades = match_order(yes_valid, no_valid, p, matched_volume)

    settlements = calculate_settlements(trades, yes_map, no_map, p)

    return {
        "clearing_price_yes": p,
        "clearing_price_no": 100 - p,
        "matched_volume": matched_volume,
        "trades": trades,
        "settlements": settlements
    }