"""MT5 order execution with fill-mode fallback: FOK -> IOC -> RETURN."""

from __future__ import annotations
import logging
import MetaTrader5 as mt5
from services.notifications.telegram import notify
from services.logging.audit_logger import log_event

logger = logging.getLogger(__name__)


def place_order(symbol: str, side: str, volume: float,
                entry: float, sl: float, tp: float) -> dict | None:
    """Place a market order with fill-mode fallback.

    Returns dict with order details on success, None on failure.
    """
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": entry,
        "sl": sl,
        "tp": tp,
        "magic": 202603,
        "comment": "MT5_FTMO_Bot",
        "type_time": mt5.ORDER_TIME_GTC,
    }

    result = None
    for filling in [
        mt5.ORDER_FILLING_FOK,
        mt5.ORDER_FILLING_IOC,
        mt5.ORDER_FILLING_RETURN,
    ]:
        request["type_filling"] = filling
        result = mt5.order_send(request)
        if result is None:
            continue
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log_event("ORDER_FILLED", {
                "ticket": result.order,
                "symbol": symbol,
                "side": side,
                "volume": volume,
                "price": result.price,
                "sl": sl,
                "tp": tp,
                "filling": filling,
            })
            return {
                "ticket": result.order,
                "price": result.price,
                "volume": result.volume,
            }
        if result.retcode == mt5.TRADE_RETCODE_REQUOTE:
            # Fetch fresh price and retry
            tick = mt5.symbol_info_tick(symbol)
            if tick:
                request["price"] = tick.ask if side == "BUY" else tick.bid
            logger.info("Requote on %s, retrying with fresh price", symbol)

    # All fill modes failed
    retcode = result.retcode if result else "NO_RESULT"
    comment = result.comment if result else "No result"
    logger.error("Order failed for %s: retcode=%s comment=%s", symbol, retcode, comment)
    log_event("ORDER_FAILED", {
        "symbol": symbol, "side": side, "volume": volume,
        "retcode": str(retcode), "comment": comment,
    })
    notify(f"❌ ORDER FAILED: {symbol} {side} {volume} lots\nCode: {retcode}\n{comment}")
    return None
