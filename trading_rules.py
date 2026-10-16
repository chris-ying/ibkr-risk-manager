"""IBKR trading restrictions, order checks and configuration.

This module is imported by order_blocker.py; do not execute it separately.
"""
import json
import math
from decimal import Decimal, InvalidOperation
import time
from pathlib import Path
from datetime import datetime, time as clock_time
from zoneinfo import ZoneInfo


HOST = "127.0.0.1"
PORT = 7496
CLIENT_ID = 0

TIMEZONE = ZoneInfo("America/Toronto")

# Market-Open Direction Lock: 
# LONG blocks SELL, SHORT blocks BUY, NONE blocks neither.
POSITION_SIDE = "NONE"
MARKET_OPEN_DIRECTION_BLOCK_START = clock_time(9, 30)
MARKET_OPEN_DIRECTION_BLOCK_END = clock_time(9, 40)

BUY_CANCEL_BLOCK_START = clock_time(9, 0)
BUY_CANCEL_BLOCK_END = clock_time(10, 00)

STOP_BUY_REMINDER_TIME = clock_time(9, 20)
STOP_BUY_REMINDER_END = clock_time(9, 30)

TUESDAY_SELL_BLOCK_START = clock_time(9, 30)
TUESDAY_SELL_BLOCK_END = clock_time(9, 55)

NEW_SHORT_BLOCK_START = clock_time(9, 30)
NEW_SHORT_BLOCK_END = clock_time(16, 0)

OVERNIGHT_START = clock_time(20, 0)
OVERNIGHT_END = clock_time(4, 0)
MAX_OVERNIGHT_BUYS = 2
MAX_OVERNIGHT_SELLS = 2
OVERNIGHT_HISTORY_FILE = Path(__file__).with_name("overnight_order_history.json")

MAX_LONG_POSITION_VALUE = 6000
MAX_SHORT_POSITION_VALUE = 6000
MAX_HIGH_BETA_POSITION_VALUE = 3000
FULL_LIMIT_STOCKS = {
    "AAPL",
    "NVDA",
    "MU",
    "SKHY",
    "DRAM",
    "NBIS",
}

# The cooldown starts only after an order is completely filled.
REDUCING_POSITION_COOLDOWN_SECONDS = 5 * 60
ADDING_POSITION_COOLDOWN_SECONDS = 10 * 60

# Independent Price Overlap rule: cancel same-side limit orders strictly within 1% of the fill price.
PRICE_OVERLAP_THRESHOLD = Decimal("0.01")

BUY_ORDER_BLOCK_START = clock_time(10, 50)
BUY_ORDER_BLOCK_END = clock_time(10, 59)

MAX_OPTION_CONTRACTS_PER_TICKER = 2

AFTERNOON_RULE_START = clock_time(14, 0)
AFTERNOON_RULE_END = clock_time(15, 0)

MAX_SINGLE_TRADE_VALUE = 6000

# Automatically submit a stop order to lock in $60 profit when unrealized 
# profit exceeds both $100 and 2% of the position value for a new position.
PROFIT_TRIGGER = 100
PROFIT_TRIGGER_PERCENT = 0.02
OPTION_PROFIT_TRIGGER_PERCENT = 1.0
PROFIT_STOP_LOCK = 60
PROFIT_STOP_START = clock_time(9, 30)
PROFIT_STOP_END = clock_time(16, 0)
PROFIT_STOP_ORDER_REF = "AUTO_PROFIT_STOP"
PROFIT_POPUP_INTERVAL_SECONDS = 10 * 60

# Automatically submit a stop-loss order for a new position
# at $50 loss or 2% of the entry value, whichever is smaller.
LOSS_CUT_AMOUNT = 50
LOSS_STOP_PERCENT = 0.02
LOSS_STOP_START = clock_time(9, 30)
LOSS_STOP_END = clock_time(16, 0)
LOSS_STOP_ORDER_REF = "AUTO_LOSS_STOP"
LOSS_POPUP_LONG_POSITION_VALUE_MIN = 100
LOSS_POPUP_LONG_POSITION_VALUE_MAX = 6000
LOSS_POPUP_INTERVAL_SECONDS = 30 * 60

# Stock buybacks after a same-day sale may not exceed shares sold.
BUYBACK_EXECUTIONS_REQUEST_ID = 899998

# Read-only IBKR account-risk reminders (Toronto local time).
CASH_REMINDER_TIMES = (clock_time(15, 0), clock_time(15, 30), clock_time(15, 50))
CASH_REMINDER_MIN_RATIO = 0.50
CASH_REMINDER_FIRST_REQUEST_ID = 700000

# Below 30%, block exposure increases except option BUY orders below <$300.
MARGIN_MIN_LIQUIDITY_RATIO = 0.30
MARGIN_OPTION_BUY_MAX_VALUE = 500
MARGIN_CHECK_INTERVAL_SECONDS = 60
MARGIN_POPUP_INTERVAL_SECONDS = 30 * 60
MARGIN_SNAPSHOT_MAX_AGE_SECONDS = 30

EXCESS_SELL_POPUP_INTERVAL_SECONDS = 60


class TradingRulesMixin:

    def is_overnight(self, now=None):
        if now is None:
            now = datetime.now(TIMEZONE)

        current_time = now.time()
        return current_time >= OVERNIGHT_START or current_time < OVERNIGHT_END

    def overnight_session_key(self, now=None):
        if now is None:
            now = datetime.now(TIMEZONE)

        # The session is named by the calendar date on which it begins at 8:00 PM.
        if now.time() < OVERNIGHT_END:
            session_date = now.date().fromordinal(now.date().toordinal() - 1)
        else:
            session_date = now.date()

        return session_date.isoformat()

    def load_overnight_order_history(self):
        try:
            if not OVERNIGHT_HISTORY_FILE.exists():
                return

            data = json.loads(OVERNIGHT_HISTORY_FILE.read_text())
            session_key = self.overnight_session_key()

            if data.get("session") == session_key:
                self.overnight_order_history = {
                    "BUY": int(data.get("BUY", 0)),
                    "SELL": int(data.get("SELL", 0)),
                }
                print(
                    "Restored overnight order counts: "
                    f"BUY={self.overnight_order_history['BUY']}/"
                    f"{MAX_OVERNIGHT_BUYS}, "
                    f"SELL={self.overnight_order_history['SELL']}/"
                    f"{MAX_OVERNIGHT_SELLS}."
                )
            else:
                self.overnight_order_history = {"BUY": 0, "SELL": 0}
        except Exception as exc:
            print(f"Could not load overnight order-history file: {exc}")
            self.overnight_order_history = {"BUY": 0, "SELL": 0}

    def save_overnight_order_history(self):
        try:
            session_key = self.overnight_session_key()
            data = {
                "session": session_key,
                "BUY": int(self.overnight_order_history.get("BUY", 0)),
                "SELL": int(self.overnight_order_history.get("SELL", 0)),
            }
            OVERNIGHT_HISTORY_FILE.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            print(f"Could not save overnight order-history file: {exc}")

    def refresh_overnight_session(self, now=None):
        if now is None:
            now = datetime.now(TIMEZONE)

        session_key = self.overnight_session_key(now)

        try:
            if OVERNIGHT_HISTORY_FILE.exists():
                data = json.loads(OVERNIGHT_HISTORY_FILE.read_text())
                stored_session = data.get("session")
            else:
                stored_session = None
        except Exception:
            stored_session = None

        if stored_session != session_key:
            self.overnight_order_history = {"BUY": 0, "SELL": 0}
            self.save_overnight_order_history()

    def overnight_limit_reached(self, action, now=None):
        if action not in {"BUY", "SELL"}:
            return False

        if now is None:
            now = datetime.now(TIMEZONE)

        if not self.is_overnight(now):
            return False

        self.refresh_overnight_session(now)

        maximum = (
            MAX_OVERNIGHT_BUYS
            if action == "BUY"
            else MAX_OVERNIGHT_SELLS
        )
        current_count = int(self.overnight_order_history.get(action, 0))

        return current_count >= maximum

    def record_overnight_order(self, action, symbol, order_id, now=None):
        if action not in {"BUY", "SELL"}:
            return

        if now is None:
            now = datetime.now(TIMEZONE)

        if not self.is_overnight(now):
            return

        self.refresh_overnight_session(now)
        self.overnight_order_history[action] = (
            int(self.overnight_order_history.get(action, 0)) + 1
        )
        self.save_overnight_order_history()

        maximum = (
            MAX_OVERNIGHT_BUYS
            if action == "BUY"
            else MAX_OVERNIGHT_SELLS
        )

        print(
            f"Overnight {action} slot used by order {order_id} for {symbol}: "
            f"{self.overnight_order_history[action]}/{maximum}."
        )

    def buyback_remaining_shares(self, con_id):
        """Return None before any sale, otherwise remaining same-day buyback capacity."""
        executions = sorted(
            (timestamp, exec_id, side, shares)
            for exec_id, (trade_con_id, timestamp, side, shares)
            in self.buyback_executions.items()
            if trade_con_id == con_id
        )
        sold = 0.0
        bought_back = 0.0
        saw_sale = False
        for _, _, side, shares in executions:
            if side in {"SLD", "SELL"}:
                saw_sale = True
                sold += shares
            elif saw_sale:
                bought_back += shares
        return max(0.0, sold - bought_back) if saw_sale else None

    def option_long_contracts_for_symbol(self, symbol):
        total = 0.0
        for con_id, position in self.positions.items():
            contract = self.position_contracts.get(con_id)
            if contract is None:
                continue
            if str(contract.secType).upper() != "OPT":
                continue
            if str(contract.symbol).upper() != symbol:
                continue
            if position > 1e-9:
                total += position
        return total

    def option_pending_buy_contracts_for_symbol(self, symbol, exclude_order_id=None):
        total = 0.0
        for pending_order_id, pending in self.active_stock_orders.items():
            if pending_order_id == exclude_order_id:
                continue
            if pending.get("securityType") != "OPT":
                continue
            if pending.get("symbol") != symbol:
                continue
            if pending.get("action") == "BUY":
                total += float(pending.get("quantity", 0.0))
        return total

    def openOrder(self, order_id, contract, order, order_state):
        now = datetime.now(TIMEZONE)

        # Reserve an order ID above any order IDs reported by TWS.
        if self.next_order_id is not None and order_id >= self.next_order_id:
            self.next_order_id = order_id + 1

        con_id = int(contract.conId)
        security_type = str(contract.secType).upper()
        symbol = str(contract.symbol).upper()
        action = str(order.action).upper()
        order_type = str(order.orderType).upper()
        status = str(order_state.status)
        quantity = float(order.totalQuantity)


        details = self.order_details.setdefault(
            order_id,
            {
                "conId": con_id,
                "symbol": symbol,
                "action": action,
                "securityType": security_type,
                "orderType": order_type,
                "orderRef": str(getattr(order, "orderRef", "")),
                "completedRecorded": False,
            },
        )

        details["conId"] = con_id
        details["symbol"] = symbol
        details["action"] = action
        details["securityType"] = security_type
        details["orderType"] = order_type
        details["orderRef"] = str(getattr(order, "orderRef", ""))
        details["account"] = str(getattr(order, "account", "") or "")
        # Snapshot whether this order reduces a position before it executes.
        if "reducingAtSubmission" not in details:
            held = self.positions.get(con_id, 0.0)
            details["reducingAtSubmission"] = (
                (held > 1e-9 and action == "SELL" and quantity <= held + 1e-9)
                or (held < -1e-9 and action == "BUY" and quantity <= abs(held) + 1e-9)
            )
        if security_type == "STK" and details["orderRef"] == PROFIT_STOP_ORDER_REF:
            self.automatic_profit_stops[con_id] = order_id
        if security_type == "STK" and details["orderRef"] == LOSS_STOP_ORDER_REF:
            self.automatic_loss_stops[con_id] = order_id

        log_key = (
            order_id,
            symbol,
            action,
            order_type,
            quantity,
            str(getattr(order, "lmtPrice", "")),
            status,
        )

        if log_key not in self.logged_orders:
            self.logged_orders.add(log_key)


        if (
            security_type == "STK"
            and action == "SELL"
            and order_id != 0
            and status in {"PreSubmitted", "Submitted"}
        ):
            self.active_sell_orders[order_id] = {
                "conId": con_id,
                "quantity": quantity,
            }

        if (
            security_type in {"STK", "OPT"}
            and action in {"BUY", "SELL"}
            and order_id != 0
            and status in {"PreSubmitted", "Submitted"}
        ):
            self.active_stock_orders[order_id] = {
                "conId": con_id,
                "symbol": symbol,
                "action": action,
                "quantity": quantity,
                "securityType": security_type,
                "orderType": order_type,
                "orderRef": str(getattr(order, "orderRef", "")),
                "account": str(getattr(order, "account", "") or ""),
                "checkPrice": (float(getattr(order, "lmtPrice", 0) or 0) if order_type in {"LMT", "STP LMT"} else float(getattr(order, "auxPrice", 0) or 0) if order_type == "STP" else 0.0),
                "multiplier": (float(getattr(contract, "multiplier", 0) or 0) if security_type == "OPT" else 1.0),
            }

        # Monitor all working stock SELL orders (LMT, STP, STP LMT, etc.).
        # This tracking is read-only and independent of entry restrictions.
        if security_type == "STK" and action == "SELL" and order_id != 0:
            if status in {"PreSubmitted", "Submitted", "PendingSubmit", "ApiPending", "PendingCancel"}:
                self.pending_stock_sell_orders[order_id] = {
                    "conId": con_id, "symbol": symbol, "remaining": quantity,
                }
            else:
                self.pending_stock_sell_orders.pop(order_id, None)

        if not self.initial_orders_loaded:
            if order_id != 0:
                self.initial_order_ids.add(order_id)
            return

        if order_id == 0:
            return

        if order_id in self.initial_order_ids:
            return

        if order_id in self.processed_orders:
            return

        if status not in {"PreSubmitted", "Submitted"}:
            return

        self.processed_orders.add(order_id)

        # Internal protective stops must not be rejected by unrelated entry restrictions.
        if security_type == "STK" and details["orderRef"] in {PROFIT_STOP_ORDER_REF, LOSS_STOP_ORDER_REF}:
            print(f"Protective stop accepted: {symbol} order {order_id}.")
            return

        current_position = self.positions.get(con_id, 0.0)

        # New exposure-increasing orders require sufficient liquidity, except
        # option BUY orders with verifiable total premium strictly below $300.
        # Existing working orders are separately inspected by the monitor loop.
        if security_type in {"STK", "OPT"} and action in {"BUY", "SELL"}:
            price = (float(getattr(order, "lmtPrice", 0) or 0)
                     if order_type in {"LMT", "STP LMT"} else
                     float(getattr(order, "auxPrice", 0) or 0)
                     if order_type == "STP" else 0.0)
            multiplier = (float(getattr(contract, "multiplier", 0) or 0)
                          if security_type == "OPT" else 1.0)
            if self.margin_order_should_block(con_id, security_type, action, quantity,
                                              details["orderRef"], price, multiplier):
                ratio = self.margin_liquidity_ratio(details["account"], con_id)
                if ratio is None or ratio < MARGIN_MIN_LIQUIDITY_RATIO:
                    level = "unavailable/stale" if ratio is None else f"{ratio:.1%}"
                    print(f"30% margin guard: cancelling new {symbol} {action} order {order_id}; "
                          f"liquidity ratio={level}; option BUY exception is strictly under $300.")
                    self.request_cancel(order_id)
                    return

        # Once shares of a stock have been sold today, prevent buying back
        # more shares than sold, including previous buybacks and pending BUYs.
        # Orders already open when this script started are skipped above.
        if security_type == "STK" and action == "BUY":
            if self.buyback_history_loaded:
                remaining_buyback = self.buyback_remaining_shares(con_id)
                if remaining_buyback is not None:
                    pending_buy_quantity = sum(
                        float(pending.get("quantity", 0.0))
                        for pending_id, pending in self.active_stock_orders.items()
                        if pending_id != order_id and pending.get("conId") == con_id
                        and pending.get("action") == "BUY"
                        and pending.get("securityType") == "STK"
                    )
                    available = max(0.0, remaining_buyback - pending_buy_quantity)
                    if quantity > available + 1e-9:
                        print(f"Same-day buyback cap: cancelling new {symbol} BUY order {order_id}; "
                              f"requested={quantity:g}, available={available:g} shares.")
                        self.request_cancel(order_id)
                        return

        # Limit each newly submitted stock/option trade to $6,000 notional.
        # This is per order, not the total position value. Startup orders are
        # protected by the initial_order_ids guard above.
        if security_type in {"STK", "OPT"} and action in {"BUY", "SELL"}:
            if order_type in {"LMT", "STP LMT"}:
                check_price = float(getattr(order, "lmtPrice", 0) or 0)
            elif order_type == "STP":
                check_price = float(getattr(order, "auxPrice", 0) or 0)
            else:
                check_price = 0.0

            multiplier = 1.0
            if security_type == "OPT":
                try:
                    multiplier = float(getattr(contract, "multiplier", 0) or 0)
                except (TypeError, ValueError):
                    multiplier = 0.0

            if (not math.isfinite(check_price) or check_price <= 0
                    or not math.isfinite(multiplier) or multiplier <= 0):
                print(f"Single-trade cap: cannot verify order value for {symbol} "
                      f"{action} order {order_id} ({order_type}); cancelling new order.")
                self.request_cancel(order_id)
                return

            trade_value = abs(quantity) * check_price * multiplier
            if trade_value > MAX_SINGLE_TRADE_VALUE + 1e-9:
                print(f"Single-trade cap: cancelling new {symbol} {action} order "
                      f"{order_id}; trade value=${trade_value:,.2f} exceeds "
                      f"${MAX_SINGLE_TRADE_VALUE:,.0f}.")
                self.request_cancel(order_id)
                return

        # From 14:00 to 15:00, stock orders may only reduce an existing position.
        # Never touch orders that were already open when the blocker started.
        # Account for other pending closing orders to avoid reversing exposure.
        if (security_type == "STK"
                and action in {"BUY", "SELL"}
                and AFTERNOON_RULE_START <= now.time() < AFTERNOON_RULE_END):
            is_closing_side = (
                (current_position > 1e-9 and action == "SELL")
                or (current_position < -1e-9 and action == "BUY")
            )
            pending_closing_quantity = sum(
                float(pending.get("quantity", 0.0))
                for pending_id, pending in self.active_stock_orders.items()
                if pending_id != order_id
                and pending.get("conId") == con_id
                and pending.get("securityType") == "STK"
                and pending.get("action") == action
            )
            remaining_to_close = max(0.0, abs(current_position) - pending_closing_quantity)
            if not is_closing_side or quantity > remaining_to_close + 1e-9:
                print(
                    f"Afternoon reduce-only restriction: cancelling new {symbol} "
                    f"{action} order {order_id}. Position={current_position:g}, "
                    f"available to reduce={remaining_to_close:g} shares."
                )
                self.request_cancel(order_id)
                return

        # Block BUY orders from 10:50:00 up to, but not including, 10:59:00.
        if (
            security_type in {"STK", "OPT"}
            and action == "BUY"
            and BUY_ORDER_BLOCK_START <= now.time() < BUY_ORDER_BLOCK_END
        ):
            print(
                f"BUY-order time block triggered: "
                f"Cancelling BUY order {order_id} for {symbol}. "
                f"BUY orders are blocked from "
                f"{BUY_ORDER_BLOCK_START.strftime('%H:%M')} to "
                f"{BUY_ORDER_BLOCK_END.strftime('%H:%M')}."
            )
            self.request_cancel(order_id)
            return

        if security_type == "OPT" and action == "SELL":
            right = str(getattr(contract, "right", "")).upper()

            # Block opening or increasing a short CALL position.
            # Selling an existing long CALL to reduce/close it is still allowed.
            if right == "C":
                is_reducing_long_call = (
                    current_position > 1e-9
                    and quantity <= current_position + 1e-9
                )

                if not is_reducing_long_call:
                    print(
                        f"Short CALL blocked: cancelling {symbol} SELL order {order_id}. "
                        "Opening or increasing a short CALL position is not allowed."
                    )
                    self.request_cancel(order_id)
                    return

        if security_type == "OPT" and action == "BUY":
            # Buying to reduce/close an existing short option position is allowed.
            is_reducing_short_option = current_position < -1e-9 and quantity <= abs(current_position) + 1e-9

            if not is_reducing_short_option:
                # No option averaging down: if this exact option contract is already
                # a losing long position, block another BUY on the same contract.
                if current_position > 1e-9:
                    unrealized = self.unrealized_pnls.get(con_id)

                    if unrealized is None or not math.isfinite(unrealized):
                        print(
                            f"Option add-on blocked: {symbol} already has this long option contract, "
                            "but its unrealized P&L is unavailable. "
                            "Cancelling the BUY order rather than allowing a possible average-down."
                        )
                        self.request_cancel(order_id)
                        return

                    if unrealized < 0:
                        print(
                            f"Option averaging down blocked: {symbol} existing option position "
                            f"is losing ${abs(unrealized):.0f}. "
                            f"Cancelling BUY order {order_id}."
                        )
                        self.request_cancel(order_id)
                        return

                owned_contracts = self.option_long_contracts_for_symbol(symbol)
                pending_buy_contracts = self.option_pending_buy_contracts_for_symbol(
                    symbol, exclude_order_id=order_id
                )
                projected_contracts = owned_contracts + pending_buy_contracts + quantity

                if projected_contracts > MAX_OPTION_CONTRACTS_PER_TICKER + 1e-9:
                    print(
                        f"Option contract limit exceeded: {symbol} would have "
                        f"{projected_contracts:g} contracts including pending BUY orders. "
                        f"Maximum={MAX_OPTION_CONTRACTS_PER_TICKER}, regardless of strike or expiration."
                    )
                    self.request_cancel(order_id)
                    return

        # Block a newly submitted SELL if that order alone would open a short.
        # Do not count other unfilled SELL orders in this specific rule.
        # Other safeguards (including the afternoon reduce-only rule) are unchanged.
        if (
            security_type in {"STK", "OPT"}
            and action == "SELL"
            and NEW_SHORT_BLOCK_START <= now.time() < NEW_SHORT_BLOCK_END
            and current_position >= -1e-9
        ):
            if quantity > current_position + 1e-9:
                print(
                    f"New-short entry blocked: cancelling {symbol} SELL order {order_id}. "
                    f"Current position={current_position:g}, "
                    f"new SELL quantity={quantity:g}. "
                    f"Other unfilled SELL orders are excluded from this check. "
                    f"SELL orders that individually could open a new short position are blocked from "
                    f"{NEW_SHORT_BLOCK_START.strftime('%H:%M')} to "
                    f"{NEW_SHORT_BLOCK_END.strftime('%H:%M')}. "
                    "Adding to an already-short position is allowed."
                )
                self.request_cancel(order_id)
                return

        if (
            security_type in {"STK", "OPT"}
            and action == "SELL"
            and now.weekday() == 1
            and TUESDAY_SELL_BLOCK_START <= now.time() < TUESDAY_SELL_BLOCK_END
        ):
            print(
                f"Tuesday SELL restriction triggered. "
                f"Cancelling SELL order {order_id} for {symbol}. "
                f"SELL orders are blocked from "
                f"{TUESDAY_SELL_BLOCK_START.strftime('%H:%M')} to {TUESDAY_SELL_BLOCK_END.strftime('%H:%M')}."
            )
            self.request_cancel(order_id)
            return

        # From 09:30 to 09:40, enforce the manually selected market direction.
        # LONG -> block SELL, SHORT -> block BUY, NONE -> block neither.
        blocked_market_open_action = {"LONG": "SELL", "SHORT": "BUY"}.get(POSITION_SIDE.upper())
        if (
            security_type in {"STK", "OPT"}
            and action == blocked_market_open_action
            and MARKET_OPEN_DIRECTION_BLOCK_START <= now.time() < MARKET_OPEN_DIRECTION_BLOCK_END
        ):
            print(
                f"Market-open direction lock triggered: POSITION_SIDE={POSITION_SIDE.upper()}. "
                f"Cancelling {symbol} {action} order {order_id}. "
                f"{action} orders are blocked from "
                f"{MARKET_OPEN_DIRECTION_BLOCK_START.strftime('%H:%M')} to "
                f"{MARKET_OPEN_DIRECTION_BLOCK_END.strftime('%H:%M')}."
            )
            self.request_cancel(order_id)
            return

        if (
            security_type == "STK"
            and action in {"BUY", "SELL"}
            and self.is_overnight(now)
        ):
            if self.overnight_limit_reached(action, now):
                maximum = (
                    MAX_OVERNIGHT_BUYS
                    if action == "BUY"
                    else MAX_OVERNIGHT_SELLS
                )

                print(
                    f"Overnight {action} limit reached. "
                    f"Cancelling order {order_id} for {symbol}. "
                    f"Only {maximum} {action} order is allowed per "
                    f"overnight session "
                    f"({OVERNIGHT_START.strftime('%H:%M')}–"
                    f"{OVERNIGHT_END.strftime('%H:%M')})."
                )

                self.request_cancel(order_id)
                return

            # Reserve the overnight slot immediately when the order is accepted,
            # so a second order cannot slip through before the first one fills.
            self.record_overnight_order(action, symbol, order_id, now)

        rule_key = (con_id, action)
        completed_at = self.last_completed_order_times.get(rule_key)
        if completed_at is not None:
            elapsed = time.monotonic() - completed_at

            # A reduction must move an existing position toward zero without reversing it.
            is_reducing_position = (
                (current_position > 1e-9 and action == "SELL" and quantity <= current_position + 1e-9)
                or (current_position < -1e-9 and action == "BUY" and quantity <= abs(current_position) + 1e-9)
            )
            cooldown_seconds = self.last_completed_order_cooldowns.get(
                rule_key,
                REDUCING_POSITION_COOLDOWN_SECONDS if is_reducing_position else ADDING_POSITION_COOLDOWN_SECONDS,
            )

            if elapsed < cooldown_seconds:
                remaining = max(0, int(cooldown_seconds - elapsed))
                minutes = remaining // 60
                seconds = remaining % 60
                cooldown_minutes = cooldown_seconds // 60

                print(
                    f"{symbol} {action} order {order_id} rejected by "
                    f"the completed-order {cooldown_minutes}-minute cooldown. "
                    f"Wait {minutes}m {seconds}s."
                )

                self.request_cancel(order_id)
                return

        other_pending_buy_quantity = sum(
            pending_order.get("quantity", 0.0)
            for pending_order_id, pending_order
            in self.active_stock_orders.items()
            if pending_order_id != order_id
            and pending_order["conId"] == con_id
            and pending_order["action"] == "BUY"
        )

        other_pending_sell_quantity = sum(
            pending_order.get("quantity", 0.0)
            for pending_order_id, pending_order
            in self.active_stock_orders.items()
            if pending_order_id != order_id
            and pending_order["conId"] == con_id
            and pending_order["action"] == "SELL"
        )

        # Check each side independently: a pending SELL is not guaranteed to
        # fill before a new BUY. Never credit pending SELLs against BUY exposure.
        if action == "BUY":
            position_before_this_order = current_position + other_pending_buy_quantity
            projected_position = position_before_this_order + quantity
        else:
            position_before_this_order = (
                current_position + other_pending_buy_quantity - other_pending_sell_quantity
            )
            projected_position = position_before_this_order - quantity

        if security_type == "STK" and action in {"BUY", "SELL"}:
            # Apply the side-specific position-value limit only when THIS order increases absolute exposure.
            # Reducing or closing a position is always allowed by the position-value rule,
            # even when the remaining position is still above the applicable limit.
            is_increasing_absolute_position = (
                abs(projected_position) > abs(position_before_this_order) + 1e-9
            )

            if is_increasing_absolute_position and abs(projected_position) > 1e-9:
                # A limit price is required only for orders that increase exposure,
                # so the projected dollar value can be verified.
                if order_type not in {"LMT", "STP LMT"}:
                    print(
                        f"Position-value check could not be verified: "
                        f"{symbol} {action} order {order_id} is {order_type}. "
                        "Use LMT or STP LMT when increasing a position so the "
                        "projected position value can be checked."
                    )

                    self.request_cancel(order_id)
                    return

                limit_price = float(order.lmtPrice)

                if limit_price <= 0:
                    print(
                        f"Invalid limit price. "
                        f"Cancelling order {order_id}."
                    )

                    self.request_cancel(order_id)
                    return

                projected_position_value = abs(projected_position) * limit_price
                projected_side = "LONG" if projected_position > 0 else "SHORT"
                max_allowed_position_value = (
                    MAX_SHORT_POSITION_VALUE
                    if projected_position < -1e-9
                    else (MAX_LONG_POSITION_VALUE if symbol in FULL_LIMIT_STOCKS else MAX_HIGH_BETA_POSITION_VALUE)
                )

                if projected_position_value > max_allowed_position_value:
                    print(
                        f"Position-value limit exceeded while increasing position: "
                        f"{symbol}, "
                        f"projected {projected_side.lower()} shares="
                        f"{abs(projected_position):.0f}, "
                        f"price=${limit_price:.0f}, "
                        f"projected value=${projected_position_value:.0f}, "
                        f"maximum=${max_allowed_position_value:.0f}."
                    )

                    self.request_cancel(order_id)
                    return

    def cancel_price_overlap_orders_after_fill(self, filled_order_id, con_id, action, symbol, fill_price):
        """Price Overlap: cancel same-contract limit orders strictly <1% from fill.

        This rule is independent of the 5/10-minute cooldown. Orders
        present at startup are never cancelled. Protective stops are
        never cancelled by this rule. If the fill price is unavailable, leave
        other orders untouched rather than guessing.
        """
        try:
            executed_price = Decimal(str(fill_price))
        except (InvalidOperation, TypeError, ValueError):
            return
        if not executed_price.is_finite() or executed_price <= 0:
            return

        for pending_id, pending in list(self.active_stock_orders.items()):
            if (pending_id == filled_order_id or pending_id == 0
                    or pending.get("conId") != con_id
                    or pending.get("action") != action
                    or pending_id in self.cancel_requested
                    or pending_id in self.initial_order_ids):
                continue
            # Only regular limit orders qualify. Neither automatic nor manual
            # STP / STP LMT protective orders should be cancelled here.
            if (pending.get("orderRef") in {LOSS_STOP_ORDER_REF, PROFIT_STOP_ORDER_REF}
                    or pending.get("orderType") != "LMT"):
                continue
            try:
                pending_price = Decimal(str(pending.get("checkPrice", 0)))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if not pending_price.is_finite() or pending_price <= 0:
                continue
            if abs(pending_price - executed_price) / executed_price >= PRICE_OVERLAP_THRESHOLD:
                continue

            self.cancel_requested.add(pending_id)
            try:
                self.cancelOrder(pending_id)
                print(f"Price Overlap: {action} order filled at ${executed_price}, requesting cancellation of {symbol} "
                      f"{action} limit order {pending_id} at ${pending_price}")
            except Exception as exc:
                self.cancel_requested.discard(pending_id)
                print(f"Price Overlap: could not cancel {symbol} {action} order {pending_id}: {exc}")
