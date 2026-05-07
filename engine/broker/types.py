"""Typed Pydantic models for Public.com API requests and responses.

Schemas reflect Public's documented API as of 2026-05-06. Where docs are
ambiguous (e.g. Greeks in the option chain), fields are optional so the
client is robust to either presence or absence.

References:
  - https://public.com/api/docs/templates/place-multi-leg-options-order
  - https://public.com/api/docs/resources/order-placement/preflight-multi-leg
  - https://public.com/api/docs/resources/account-details/get-account-portfolio-v2
  - https://public.com/api/docs/changelog (Greeks added to option-chain)
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _coerce_to_str(v: Any) -> Any:
    """Public's API mixes ints/floats and strings for numeric fields
    (e.g. volume=0 vs openInterest="123"). Coerce numeric inputs to str so
    schema validation accepts either form."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return str(v)
    return v


NumericStr = Annotated[Optional[str], BeforeValidator(_coerce_to_str)]

# ───────────────────────── Enums (as Literal for ergonomics) ─────────────────

InstrumentType = Literal["EQUITY", "OPTION", "INDEX_OPTION", "BOND", "CRYPTO"]
OrderSide = Literal["BUY", "SELL"]
OptionType = Literal["CALL", "PUT"]
OpenClose = Literal["OPEN", "CLOSE"]
OrderType = Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT"]
TimeInForce = Literal["DAY", "GTC"]
OrderStatus = Literal[
    "NEW", "ACCEPTED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED", "EXPIRED"
]
AccountType = Literal[
    "BROKERAGE", "HIGH_YIELD", "BOND_ACCOUNT", "RIA_ASSET",
    "TREASURY", "TRADITIONAL_IRA", "ROTH_IRA",
]
BrokerageAccountType = Literal["MARGIN", "CASH"]
# Public returns 'NONE' for accounts that have options disabled (e.g. IRA/cash).
OptionsLevel = Literal["NONE", "LEVEL_0", "LEVEL_1", "LEVEL_2", "LEVEL_3", "LEVEL_4"]


# ───────────────────────── Common building blocks ────────────────────────────

class Instrument(BaseModel):
    model_config = ConfigDict(extra="allow")
    symbol: str
    type: InstrumentType


class Expiration(BaseModel):
    timeInForce: TimeInForce = "DAY"
    expirationTime: Optional[str] = None  # ISO-8601 if GTC


class OptionDetails(BaseModel):
    model_config = ConfigDict(extra="allow")
    baseSymbol: str
    type: OptionType
    strikePrice: str  # Public returns these as strings
    optionExpireDate: str  # YYYY-MM-DD


# ───────────────────────── Account ───────────────────────────────────────────

class Account(BaseModel):
    model_config = ConfigDict(extra="allow")
    accountId: str
    accountType: AccountType
    optionsLevel: Optional[OptionsLevel] = None
    brokerageAccountType: Optional[BrokerageAccountType] = None


class AccountListResponse(BaseModel):
    accounts: list[Account]


# ───────────────────────── Option chain ──────────────────────────────────────

class OptionChainEntry(BaseModel):
    """A single contract in the chain. Greeks are present per Public's
    changelog but not documented in the schema page yet — treat as optional."""
    model_config = ConfigDict(extra="allow")
    instrument: Instrument
    outcome: Optional[str] = None  # "SUCCESS" / error
    last: NumericStr = None
    bid: NumericStr = None
    ask: NumericStr = None
    volume: NumericStr = None
    openInterest: NumericStr = None
    impliedVolatility: NumericStr = None
    delta: NumericStr = None
    gamma: NumericStr = None
    theta: NumericStr = None
    vega: NumericStr = None
    rho: NumericStr = None
    strikePrice: NumericStr = None
    optionType: Optional[OptionType] = None
    expirationDate: Optional[str] = None


class OptionChainRequest(BaseModel):
    instrument: Instrument
    expirationDate: str  # YYYY-MM-DD


class OptionChainResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    baseSymbol: str
    calls: list[OptionChainEntry] = Field(default_factory=list)
    puts: list[OptionChainEntry] = Field(default_factory=list)


# ───────────────────────── Option expirations ────────────────────────────────

class OptionExpirationsRequest(BaseModel):
    instrument: Instrument


class OptionExpirationsResponse(BaseModel):
    baseSymbol: str
    expirations: list[str]


# ───────────────────────── Quotes ────────────────────────────────────────────

class QuoteRequest(BaseModel):
    instruments: list[Instrument]


class Quote(BaseModel):
    model_config = ConfigDict(extra="allow")
    instrument: Instrument
    last: Optional[str] = None
    bid: Optional[str] = None
    ask: Optional[str] = None
    bidSize: Optional[str] = None
    askSize: Optional[str] = None


class QuoteResponse(BaseModel):
    quotes: list[Quote]


# ───────────────────────── Multi-leg orders ──────────────────────────────────

class OrderLeg(BaseModel):
    instrument: Instrument
    side: OrderSide
    openCloseIndicator: OpenClose
    ratioQuantity: int = 1


class MultiLegPreflightRequest(BaseModel):
    """POST /trading/{accountId}/preflight/multi-leg

    Note: quantity is a string in preflight per Public's docs."""
    orderType: OrderType = "LIMIT"
    expiration: Expiration = Field(default_factory=lambda: Expiration(timeInForce="DAY"))
    quantity: str
    limitPrice: str
    legs: list[OrderLeg]
    validateOrder: Optional[bool] = True


class RegulatoryFees(BaseModel):
    model_config = ConfigDict(extra="allow")
    secFee: Optional[str] = None
    tafFee: Optional[str] = None
    orfFee: Optional[str] = None
    exchangeFee: Optional[str] = None
    occFee: Optional[str] = None
    catFee: Optional[str] = None


class PreflightLeg(OrderLeg):
    optionDetails: Optional[OptionDetails] = None


class MultiLegPreflightResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    baseSymbol: str
    strategyName: Optional[str] = None
    legs: list[PreflightLeg]
    estimatedCommission: Optional[str] = None
    regulatoryFees: Optional[RegulatoryFees] = None
    estimatedIndexOptionFee: Optional[str] = None
    orderValue: str
    estimatedQuantity: Optional[str] = None
    estimatedCost: Optional[str] = None
    buyingPowerRequirement: Optional[str] = None
    estimatedProceeds: Optional[str] = None


class MultiLegOrderRequest(BaseModel):
    """POST /trading/{accountId}/order/multileg

    Note: quantity is an integer here (per Public's example), unlike preflight."""
    orderId: str  # UUID v4
    quantity: int
    type: OrderType = "LIMIT"
    limitPrice: str
    expiration: Expiration = Field(default_factory=lambda: Expiration(timeInForce="DAY"))
    legs: list[OrderLeg]


class MultiLegOrderResponse(BaseModel):
    orderId: str


# ───────────────────────── Order status ──────────────────────────────────────

class OrderStatusResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    orderId: str
    instrument: Optional[Instrument] = None
    createdAt: Optional[str] = None
    type: Optional[OrderType] = None
    side: Optional[OrderSide] = None
    status: OrderStatus
    quantity: Optional[str] = None
    notionalValue: Optional[str] = None
    expiration: Optional[Expiration] = None
    limitPrice: Optional[str] = None
    stopPrice: Optional[str] = None
    closedAt: Optional[str] = None
    openCloseIndicator: Optional[OpenClose] = None
    filledQuantity: Optional[str] = None
    averagePrice: Optional[str] = None
    legs: Optional[list[OrderLeg]] = None
    rejectReason: Optional[str] = None


# ───────────────────────── Portfolio ─────────────────────────────────────────

class CostBasis(BaseModel):
    model_config = ConfigDict(extra="allow")
    totalCost: Optional[str] = None
    unitCost: Optional[str] = None
    gainValue: Optional[str] = None
    gainPercentage: Optional[str] = None


class Position(BaseModel):
    model_config = ConfigDict(extra="allow")
    instrument: Instrument
    quantity: str
    currentValue: Optional[str] = None
    percentOfPortfolio: Optional[str] = None
    costBasis: Optional[CostBasis] = None


class BuyingPower(BaseModel):
    model_config = ConfigDict(extra="allow")
    cashOnlyBuyingPower: Optional[str] = None
    buyingPower: Optional[str] = None
    optionsBuyingPower: Optional[str] = None


class EquitySummary(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str
    value: str
    percentageOfPortfolio: Optional[str] = None


class PortfolioResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    accountId: str
    accountType: AccountType
    buyingPower: BuyingPower
    equity: list[EquitySummary] = Field(default_factory=list)
    positions: list[Position] = Field(default_factory=list)
    orders: list[OrderStatusResponse] = Field(default_factory=list)
