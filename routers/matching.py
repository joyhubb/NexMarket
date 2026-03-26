from fastapi import APIRouter
from typing import List, Optional, Dict
from pydantic import BaseModel, Field
from datetime import datetime
from collections import defaultdict
import copy

router = APIRouter()

MATCHING_TYPE_CROSS = "cross"
MATCHING_TYPE_sAME = "same side"

SIDE_YES = "yes"
SIDE_NO = "no"

# =========================
# MODELS
# =========================

class Order(BaseModel):
    id: str
    user_id: str
    price: int = Field(ge=1, le=99)
    remain: int = Field(gt=0)
    created_at: datetime

class Trade(BaseModel):
    type: str 

    user_id_a: str 
    user_id_b: str 

    order_id_a: str 
    order_id_b: str    

    outcome: Optional[str] = None 

    price: int
    quantity: int

class MatchingRequest(BaseModel):
    option_id: str
    orders_buy_yes: List[Order]
    orders_sell_yes: List[Order]
    orders_buy_no: List[Order]
    orders_sell_no: List[Order]

class UserUpdate(BaseModel):
    user_id: str
    spent: int 
    received: int
    yes_delta: int
    no_delta: int
