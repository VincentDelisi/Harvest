"""Public.com REST API client for the credit spread engine.

Implements every endpoint the engine needs:
  - GET    /userapigateway/trading/account                          → account info
  - POST   /userapigateway/marketdata/{acct}/option-expirations     → expirations
  - POST   /userapigateway/marketdata/{acct}/option-chain           → chain + Greeks
  - POST   /userapigateway/marketdata/{acct}/quotes                 → real-time quotes
  - POST   /userapigateway/trading/{acct}/preflight/multi-leg       → preflight check
  - POST   /userapigateway/trading/{acct}/order/multileg            → place spread
  - GET    /userapigateway/trading/{acct}/order/{orderId}           → status / fills
  - DELETE /userapigateway/trading/{acct}/order/{orderId}           → cancel
  - GET    /userapigateway/trading/{acct}/portfolio/v2              → positions, BP

Critical safety properties:
  1. Every order goes through /preflight/multi-leg first. Aborts on rejection.
  2. dry_run=True flag short-circuits all writes and logs the would-be payload.
  3. Token auto-refresh via TokenManager.
  4. All responses are validated through Pydantic models so schema drift is caught.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from engine.broker.auth import TokenManager
from engine.broker.types import (
    AccountListResponse,
    Expiration,
    Instrument,
    MultiLegOrderRequest,
    MultiLegOrderResponse,
    MultiLegPreflightRequest,
    MultiLegPreflightResponse,
    OptionChainRequest,
    OptionChainResponse,
    OptionExpirationsRequest,
    OptionExpirationsResponse,
    OrderLeg,
    OrderStatusResponse,
    PortfolioResponse,
    Quote,
    QuoteRequest,
    QuoteResponse,
)
from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)


class PublicAPIError(RuntimeError):
    """Raised on any 4xx/5xx that we don't auto-retry."""

    def __init__(self, status: int, body: Any, url: str) -> None:
        super().__init__(f"[{status}] {url} :: {body}")
        self.status = status
        self.body = body
        self.url = url


_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def _is_retryable_http(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError))


class PublicClient:
    """Synchronous REST client for Public.com."""

    def __init__(
        self,
        account_id: Optional[str] = None,
        base_url: Optional[str] = None,
        token_manager: Optional[TokenManager] = None,
        dry_run: bool = False,
        timeout: float = 15.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.account_id = account_id or CONFIG.public_account_id
        self.base_url = (base_url or CONFIG.public_base_url).rstrip("/")
        self.token_manager = token_manager or TokenManager()
        self.dry_run = dry_run
        client_kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "timeout": timeout,
            "headers": {"Content-Type": "application/json"},
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._http = httpx.Client(**client_kwargs)

    def close(self) -> None:
        self._http.close()

    # ─────────────────────── Internal HTTP helpers ────────────────────────

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token_manager.get_token()}"}

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=10),
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
        reraise=True,
    )
    def _request(self, method: str, path: str, *, json: Any = None) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        resp = self._http.request(method, url, json=json, headers=self._auth_headers())

        # 401 → token may have expired mid-flight; invalidate & let retry mint a fresh one
        if resp.status_code == 401:
            self.token_manager.invalidate()
            resp.raise_for_status()  # triggers retry

        # Retry on 5xx/429/etc.
        if resp.status_code in _RETRYABLE_STATUS:
            resp.raise_for_status()

        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = resp.text
            raise PublicAPIError(resp.status_code, body, url)

        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ─────────────────────── Account ──────────────────────────────────────

    def get_account(self) -> AccountListResponse:
        data = self._request("GET", "/userapigateway/trading/account")
        return AccountListResponse.model_validate(data)

    def assert_options_level_at_least(self, level: str = "LEVEL_2") -> None:
        """Raise if no account has at least the specified options approval level."""
        accounts = self.get_account().accounts
        order = ["LEVEL_0", "LEVEL_1", "LEVEL_2", "LEVEL_3", "LEVEL_4"]
        required_idx = order.index(level)
        for acc in accounts:
            if acc.optionsLevel and order.index(acc.optionsLevel) >= required_idx:
                return
        raise PublicAPIError(
            403,
            {"reason": f"No account has options level ≥ {level}"},
            "/userapigateway/trading/account",
        )

    # ─────────────────────── Market data ──────────────────────────────────

    def get_option_expirations(self, symbol: str) -> OptionExpirationsResponse:
        req = OptionExpirationsRequest(instrument=Instrument(symbol=symbol, type="EQUITY"))
        data = self._request(
            "POST",
            f"/userapigateway/marketdata/{self.account_id}/option-expirations",
            json=req.model_dump(),
        )
        return OptionExpirationsResponse.model_validate(data)

    def get_option_chain(self, symbol: str, expiration_date: str) -> OptionChainResponse:
        req = OptionChainRequest(
            instrument=Instrument(symbol=symbol, type="EQUITY"),
            expirationDate=expiration_date,
        )
        data = self._request(
            "POST",
            f"/userapigateway/marketdata/{self.account_id}/option-chain",
            json=req.model_dump(),
        )
        return OptionChainResponse.model_validate(data)

    def get_quotes(self, instruments: list[Instrument]) -> QuoteResponse:
        req = QuoteRequest(instruments=instruments)
        data = self._request(
            "POST",
            f"/userapigateway/marketdata/{self.account_id}/quotes",
            json=req.model_dump(),
        )
        return QuoteResponse.model_validate(data)

    def get_quote(self, symbol: str, instrument_type: str = "EQUITY") -> Quote:
        resp = self.get_quotes([Instrument(symbol=symbol, type=instrument_type)])  # type: ignore[arg-type]
        if not resp.quotes:
            raise PublicAPIError(404, f"No quote returned for {symbol}", "/quotes")
        return resp.quotes[0]

    # ─────────────────────── Order placement ──────────────────────────────

    def preflight_multi_leg(
        self,
        legs: list[OrderLeg],
        limit_price: str,
        quantity: int,
        time_in_force: str = "DAY",
    ) -> MultiLegPreflightResponse:
        """Validate a multi-leg order before placement. STRATEGY_SPEC.md §11 step 2."""
        req = MultiLegPreflightRequest(
            orderType="LIMIT",
            expiration=Expiration(timeInForce=time_in_force),  # type: ignore[arg-type]
            quantity=str(quantity),
            limitPrice=limit_price,
            legs=legs,
            validateOrder=True,
        )
        data = self._request(
            "POST",
            f"/userapigateway/trading/{self.account_id}/preflight/multi-leg",
            json=req.model_dump(),
        )
        return MultiLegPreflightResponse.model_validate(data)

    def place_multi_leg_order(
        self,
        legs: list[OrderLeg],
        limit_price: str,
        quantity: int,
        order_id: Optional[str] = None,
        time_in_force: str = "DAY",
        skip_preflight: bool = False,
    ) -> MultiLegOrderResponse:
        """Place a multi-leg credit spread.

        STRATEGY_SPEC.md §11 enforces preflight-before-order. We default to
        running preflight here; only set skip_preflight=True if the caller has
        already validated.

        In dry_run mode, returns a fake order ID and never hits the order
        endpoint.
        """
        order_id = order_id or str(uuid.uuid4())

        if not skip_preflight:
            pre = self.preflight_multi_leg(
                legs=legs, limit_price=limit_price, quantity=quantity,
                time_in_force=time_in_force,
            )
            log.info(
                "Preflight OK: strategy=%s baseSymbol=%s buyingPowerReq=%s",
                pre.strategyName, pre.baseSymbol, pre.buyingPowerRequirement,
            )

        if self.dry_run:
            log.warning(
                "[DRY_RUN] Would place multi-leg order: id=%s qty=%d limit=%s legs=%d",
                order_id, quantity, limit_price, len(legs),
            )
            return MultiLegOrderResponse(orderId=order_id)

        req = MultiLegOrderRequest(
            orderId=order_id,
            quantity=quantity,
            type="LIMIT",
            limitPrice=limit_price,
            expiration=Expiration(timeInForce=time_in_force),  # type: ignore[arg-type]
            legs=legs,
        )
        data = self._request(
            "POST",
            f"/userapigateway/trading/{self.account_id}/order/multileg",
            json=req.model_dump(),
        )
        return MultiLegOrderResponse.model_validate(data)

    def get_order_status(self, order_id: str) -> OrderStatusResponse:
        data = self._request(
            "GET", f"/userapigateway/trading/{self.account_id}/order/{order_id}"
        )
        return OrderStatusResponse.model_validate(data)

    def cancel_order(self, order_id: str) -> None:
        if self.dry_run:
            log.warning("[DRY_RUN] Would cancel order %s", order_id)
            return
        self._request(
            "DELETE", f"/userapigateway/trading/{self.account_id}/order/{order_id}"
        )

    # ─────────────────────── Portfolio ────────────────────────────────────

    def get_portfolio(self) -> PortfolioResponse:
        data = self._request(
            "GET", f"/userapigateway/trading/{self.account_id}/portfolio/v2"
        )
        return PortfolioResponse.model_validate(data)
