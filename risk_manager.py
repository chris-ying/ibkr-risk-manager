from copy import copy
import math
import time
import signal
from datetime import datetime, time as clock_time
from threading import Event, Thread
from zoneinfo import ZoneInfo

from ibapi.execution import ExecutionFilter
from ibapi.client import EClient
from ibapi.order import Order
from ibapi.wrapper import EWrapper

import trading_rules as rules
from trading_rules import TradingRulesMixin
from auto_stops import AutoStopsMixin, profit_trigger_reached

from popups import (
    show_stop_order_reminder_popup,
    show_take_profit_popup,
    show_cut_loss_popup,
    show_excess_sell_orders_popup,
    show_trim_position_popup,
    show_margin_30_popup,
)


class OrderBlocker(TradingRulesMixin, AutoStopsMixin, EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)

        self.ready = Event()
        self.next_order_id = None
        self.automatic_profit_stops = {} 
        self.automatic_loss_stops = {} 
        self.profit_stop_transition = {} 
        self.profit_stop_ready = set()  
        self.profit_stop_snapshots = {}  
        self.new_stock_positions = set()
        self.startup_positions_captured = False
        self.stop_order_reminder_date = None
        self.cash_reminder_checked = set()
        self.cash_summary_next_id = rules.CASH_REMINDER_FIRST_REQUEST_ID
        self.cash_summary_pending = {}
        self.margin_liquidity = {}
        self.margin_popup_last_shown = {}
        self.margin_last_check = 0.0 
        self.initial_orders_loaded = False
        self.positions_loaded = False

        self.initial_order_ids = set()
        self.processed_orders = set()
        self.cancel_requested = set()
        self.logged_orders = set()

        self.positions = {}
        self.position_contracts = {}
        self.position_accounts = {}
        self.position_avg_costs = {}

        self.active_sell_orders = {}
        self.active_stock_orders = {}
        self.pending_stock_sell_orders = {}  
        self.last_excess_sell_alert_times = {}  

        self.order_details = {}

        self.last_completed_order_times = {}
        self.last_completed_order_cooldowns = {}
        self.pnl_request_symbols = {}
        self.next_pnl_request_id = 900000
        self.last_profit_popup_times = {}
        self.last_loss_popup_times = {}
        self.position_values = {}
        self.unrealized_pnls = {}
        self.option_market_values = {}

        self.buyback_executions = {}
        self.buyback_history_loaded = False


    def nextValidId(self, order_id: int):
        print("Connected to TWS.")
        print(f"Next valid API order ID: {order_id}")
        self.next_order_id = int(order_id)
        self.reqPositions()
        self.reqAutoOpenOrders(True)
        self.reqOpenOrders()
        self.reqExecutions(rules.BUYBACK_EXECUTIONS_REQUEST_ID, ExecutionFilter())
        self.request_cash_level_check("margin")
        self.ready.set()


    def execDetails(self, req_id, contract, execution):
        # Keep today's stock executions, including fills submitted from TWS.
        if str(contract.secType).upper() != "STK":
            return
        exec_id = str(execution.execId)
        if not exec_id:
            return
        execution_time = str(execution.time).strip()
        if execution_time[:8] != datetime.now(rules.TIMEZONE).strftime("%Y%m%d"):
            return
        side = str(execution.side).upper()
        if side not in {"BOT", "SLD", "BUY", "SELL"}:
            return
        # Execution reports for newly filled manual/API orders refresh position & PnL subscriptions.
        if self.buyback_history_loaded and self.positions_loaded:
            self.reqPositions()
        self.buyback_executions[exec_id] = (
            int(contract.conId), execution_time, side,
            float(execution.shares),
        )

    def execDetailsEnd(self, req_id):
        if req_id == rules.BUYBACK_EXECUTIONS_REQUEST_ID:
            self.buyback_history_loaded = True


    def has_active_stop_order(self):
        return any(
            pending.get("action") in {"BUY", "SELL"}
            and pending.get("orderType") in {"STP", "STP LMT"}
            for pending in self.active_stock_orders.values()
        )

    def request_cash_level_check(self, slot):
        """Ask IBKR for a new snapshot at each specified reminder time."""
        request_id = self.cash_summary_next_id
        self.cash_summary_next_id += 1
        self.cash_summary_pending[request_id] = {"_purpose": slot}
        try:
            self.reqAccountSummary(request_id, "All", "ExcessLiquidity,NetLiquidation")
        except Exception as exc:
            self.cash_summary_pending.pop(request_id, None)
            print(f"Could not request account liquidity: {exc}")

    def accountSummary(self, reqId, account, tag, value, currency):
        if reqId not in self.cash_summary_pending or tag not in {"ExcessLiquidity", "NetLiquidation"}:
            return
        try:
            amount = float(value)
        except (ValueError, TypeError):
            return
        if not math.isfinite(amount):
            return
        item = self.cash_summary_pending[reqId].setdefault(account, {})
        item[tag] = amount
        item[f"{tag}_currency"] = currency

    def top_three_holdings(self, account):
        """Three largest open positions by absolute IBKR-reported market value."""
        holdings = []
        for con_id, quantity in self.positions.items():
            if abs(quantity) <= 1e-9 or self.position_accounts.get(con_id) != account:
                continue
            contract = self.position_contracts.get(con_id)
            value = self.position_values.get(con_id)
            if contract is None or value is None or not math.isfinite(value):
                continue
            symbol = str(contract.symbol).upper()
            holdings.append((symbol, abs(value)))
        holdings.sort(key=lambda item: item[1], reverse=True)
        return holdings[:3]

    def accountSummaryEnd(self, reqId):
        accounts = self.cash_summary_pending.pop(reqId, None)
        if accounts is None:
            return
        try:
            self.cancelAccountSummary(reqId)
        except Exception as exc:
            print(f"Could not close account summary request {reqId}: {exc}")
        purpose = accounts.pop("_purpose", None)
        for account, data in accounts.items():
            excess = data.get("ExcessLiquidity")
            net = data.get("NetLiquidation")
            if excess is None or net is None or net <= 0:
                print(f"Skipping liquidity reminder for {account}: incomplete account values.")
                continue
            currency = data.get("NetLiquidation_currency", "")
            if currency != data.get("ExcessLiquidity_currency", ""):
                print(f"Skipping liquidity reminder for {account}: currency mismatch.")
                continue
            ratio = excess / net
            if purpose == "margin":
                checked_at = time.monotonic()
                self.margin_liquidity[account] = (ratio, checked_at)
                if ratio < rules.MARGIN_MIN_LIQUIDITY_RATIO:
                    # Alert immediately on crossing below 30%, then at most
                    # once every 10 minutes while it remains below 30%.
                    last_shown = self.margin_popup_last_shown.get(account)
                    if last_shown is None or checked_at - last_shown >= rules.MARGIN_POPUP_INTERVAL_SECONDS:
                        self.margin_popup_last_shown[account] = checked_at
                        show_margin_30_popup(account, excess, net, currency, self.top_three_holdings(account))
                    self.cancel_exposure_increasing_orders()
                else:
                    # A later drop below 30% should alert immediately again.
                    self.margin_popup_last_shown.pop(account, None)
            elif ratio < rules.CASH_REMINDER_MIN_RATIO:
                show_trim_position_popup(account, excess, net, currency, self.top_three_holdings(account))

    def margin_liquidity_ratio(self, account="", con_id=None):
        """Return a fresh account-level excess-liquidity ratio, else None."""
        account = account or (self.position_accounts.get(con_id, "") if con_id is not None else "")
        if not account and len(self.margin_liquidity) == 1:
            account = next(iter(self.margin_liquidity))
        record = self.margin_liquidity.get(account)
        if record is None or time.monotonic() - record[1] > rules.MARGIN_SNAPSHOT_MAX_AGE_SECONDS:
            return None
        return record[0]

    def margin_order_increases_exposure(self, con_id, security_type, action, quantity, order_ref=""):
        """Exempt genuine reductions; automated protective-stop references are stock-only."""
        current = float(self.positions.get(con_id, 0.0))
        quantity = float(quantity)
        reduces_long = action == "SELL" and current > 0 and quantity <= current + 1e-9
        reduces_short = action == "BUY" and current < 0 and quantity <= abs(current) + 1e-9
        if reduces_long or reduces_short:
            return False
        # An OPT order cannot bypass the liquidity guard using a stock stop reference.
        # Likewise, an oversized stock stop that could reverse the position is not exempt.
        return True

    def margin_order_should_block(self, con_id, security_type, action, quantity,
                                  order_ref="", price=0.0, multiplier=0.0):
        """Below 30%: allow reductions and option BUYs costing strictly under $300."""
        if not self.margin_order_increases_exposure(con_id, security_type, action, quantity, order_ref):
            return False
        if security_type == "OPT" and action == "BUY":
            try:
                premium = float(quantity) * float(price) * float(multiplier)
                if (math.isfinite(premium) and premium > 0
                        and math.isfinite(float(price)) and float(price) > 0
                        and math.isfinite(float(multiplier)) and float(multiplier) > 0
                        and premium < rules.MARGIN_OPTION_BUY_MAX_VALUE):
                    return False
            except (TypeError, ValueError, OverflowError):
                pass
        return True

    def cancel_exposure_increasing_orders(self):
        """Cancel exposure-increasing working orders unless an option BUY costs <$300."""
        if not (self.positions_loaded and self.initial_orders_loaded):
            return
        for order_id, pending in list(self.active_stock_orders.items()):
            if order_id == 0 or order_id in self.cancel_requested:
                continue
            # Startup orders remain untouched even below 30% liquidity.
            if order_id in self.initial_order_ids:
                continue
            if pending.get("securityType") not in {"STK", "OPT"}:
                continue
            account = pending.get("account", "")
            con_id = pending["conId"]
            ratio = self.margin_liquidity_ratio(account, con_id)
            if ratio is None or ratio >= rules.MARGIN_MIN_LIQUIDITY_RATIO:
                continue
            if not self.margin_order_should_block(
                    con_id, pending["securityType"], pending["action"],
                    pending["quantity"], pending.get("orderRef", ""),
                    pending.get("checkPrice", 0), pending.get("multiplier", 0)):
                continue
            print(f"CANCEL REQUEST: reason=30% margin guard ticker={pending['symbol']} "
                  f"side={pending['action']} order={order_id} liquidity={ratio:.1%}", flush=True)
            self.cancel_requested.add(order_id)
            try:
                self.cancelOrder(order_id)
            except Exception as exc:
                self.cancel_requested.discard(order_id)
                print(f"Margin guard: cannot cancel {order_id}: {exc}")

    def check_excess_sell_orders(self):
        """Alert every 5 minutes when combined working stock SELLs exceed held shares."""
        if not self.positions_loaded or not self.initial_orders_loaded:
            return
        now = time.monotonic()
        totals = {}
        for pending in list(self.pending_stock_sell_orders.values()):
            con_id = pending["conId"]
            remaining = max(0.0, float(pending["remaining"]))
            if remaining <= 1e-9:
                continue
            entry = totals.setdefault(con_id, {"symbol": pending["symbol"], "remaining": 0.0, "count": 0})
            entry["remaining"] += remaining
            entry["count"] += 1

        oversized = set()
        for con_id, total in totals.items():
            held = max(0.0, float(self.positions.get(con_id, 0.0)))
            if total["remaining"] > held + 1e-9:
                oversized.add(con_id)
                last = self.last_excess_sell_alert_times.get(con_id)
                if last is None or now - last >= rules.EXCESS_SELL_POPUP_INTERVAL_SECONDS:
                    self.last_excess_sell_alert_times[con_id] = now
                    show_excess_sell_orders_popup(total["symbol"], held, total["remaining"], total["count"])
        for con_id in list(self.last_excess_sell_alert_times):
            if con_id not in oversized:
                self.last_excess_sell_alert_times.pop(con_id, None)

    def stop_order_reminder_loop(self):
        while self.isConnected():
            now = datetime.now(rules.TIMEZONE)
            today = now.date()

            if rules.STOP_BUY_REMINDER_TIME <= now.time() < rules.STOP_BUY_REMINDER_END:
                if self.stop_order_reminder_date != today:
                    self.stop_order_reminder_date = today

                    if not self.has_active_stop_order():
                        show_stop_order_reminder_popup()

            for slot in rules.CASH_REMINDER_TIMES:
                # Any second during the minute is acceptable; only once daily per slot.
                key = (today, slot.hour, slot.minute)
                if now.hour == slot.hour and now.minute == slot.minute and key not in self.cash_reminder_checked:
                    self.cash_reminder_checked.add(key)
                    self.request_cash_level_check(slot)
            self.cash_reminder_checked = {key for key in self.cash_reminder_checked if key[0] == today}

            if time.monotonic() - self.margin_last_check >= rules.MARGIN_CHECK_INTERVAL_SECONDS:
                self.margin_last_check = time.monotonic()
                self.request_cash_level_check("margin")
            self.cancel_exposure_increasing_orders()
            self.check_excess_sell_orders()
            time.sleep(1)

    def position(self, account, contract, position, avg_cost):
        con_id = int(contract.conId)
        quantity = float(position)

        if str(contract.secType).upper() == "STK":
            old_quantity = self.positions.get(con_id, 0.0)
            if self.startup_positions_captured:
                if abs(quantity) <= 1e-9:
                    self.new_stock_positions.discard(con_id)
                    self.profit_stop_transition.pop(con_id, None)
                    self.profit_stop_ready.discard(con_id)
                    self.profit_stop_snapshots.pop(con_id, None)
                    self.cancel_closed_position_loss_stop(con_id)
                    self.cancel_closed_position_profit_stop(con_id)
                    self.automatic_loss_stops.pop(con_id, None)
                elif abs(old_quantity) <= 1e-9 or old_quantity * quantity < 0:
                    self.new_stock_positions.add(con_id)
            if abs(quantity) <= 1e-9:
                self.automatic_profit_stops.pop(con_id, None)
        self.position_avg_costs[con_id] = float(avg_cost)
        self.positions[con_id] = quantity
        self.position_contracts[con_id] = contract
        self.position_accounts[con_id] = account
        if self.initial_orders_loaded and con_id in self.new_stock_positions:
            self.maybe_place_loss_stop(con_id)



    def positionEnd(self):
        self.positions_loaded = True
        self.startup_positions_captured = True
        for con_id in list(self.new_stock_positions):
            self.maybe_place_loss_stop(con_id)

        for request_id in list(self.pnl_request_symbols):
            self.cancelPnLSingle(request_id)

        self.pnl_request_symbols.clear()

        for con_id, quantity in self.positions.items():
            if abs(quantity) <= 1e-9:
                continue

            account = self.position_accounts.get(con_id)
            contract = self.position_contracts.get(con_id)

            if not account or contract is None:
                continue

            symbol = str(contract.symbol).upper()
            request_id = self.next_pnl_request_id
            self.next_pnl_request_id += 1

            self.pnl_request_symbols[request_id] = {
                "conId": con_id,
                "symbol": symbol,
            }
            self.reqPnLSingle(request_id, account, "", con_id)





    def request_cancel(self, order_id):
        # Never cancel an order that was already working when the blocker started.
        # Only newly detected orders may be rejected by entry restrictions.
        if order_id == 0 or order_id in self.initial_order_ids:
            return

        if order_id in self.cancel_requested:
            return

        details = self.order_details.get(order_id, {})
        print(f"CANCEL REQUEST: order={order_id} ticker={details.get('symbol', '?')} "
              f"side={details.get('action', '?')} source=trading_rule", flush=True)
        self.cancel_requested.add(order_id)
        try:
            self.cancelOrder(order_id)
        except Exception:
            self.cancel_requested.discard(order_id)
            raise


    def openOrderEnd(self):
        if self.initial_orders_loaded:
            return

        self.initial_orders_loaded = True

        print(
            "Existing orders loaded. "
            "Order restrictions are active."
        )

    def orderStatus(
        self,
        order_id,
        status,
        filled,
        remaining,
        avg_fill_price,
        perm_id,
        parent_id,
        last_fill_price,
        client_id,
        why_held,
        mkt_cap_price=0,
    ):

        normalized_status = str(status).strip().lower()
        if normalized_status in {"cancelled", "apicancelled", "pendingcancel"}:
            detail = self.order_details.get(order_id, {})
            print(f"IBKR ORDER STATUS: order={order_id} ticker={detail.get('symbol', '?')} "
                  f"side={detail.get('action', '?')} status={status}", flush=True)
        remaining_quantity = float(remaining)

        now = datetime.now(rules.TIMEZONE)
        details = self.order_details.get(order_id)

        # Keep pending stock SELL quantities accurate after partial executions.
        if order_id in self.pending_stock_sell_orders and math.isfinite(remaining_quantity):
            self.pending_stock_sell_orders[order_id]["remaining"] = max(0.0, remaining_quantity)
        if (details is not None and details.get("securityType") == "STK"
                and details.get("action") == "SELL" and float(filled) > details.get("lastPositionRefreshFilled", 0.0)):
            details["lastPositionRefreshFilled"] = float(filled)
            self.reqPositions()

        if normalized_status == "filled" and remaining_quantity <= 1e-9:
            details = self.order_details.get(order_id)

            if details is None:
                print(
                    f"Filled order {order_id} was not found in "
                    "the local order-details cache, so no cooldown "
                    "could be recorded."
                )
            elif not details["completedRecorded"]:
                con_id = details["conId"]
                symbol = details["symbol"]
                action = details["action"]
                security_type = details["securityType"]
                rule_key = (con_id, action)

                self.last_completed_order_times[rule_key] = time.monotonic()
                cooldown_seconds = (rules.REDUCING_POSITION_COOLDOWN_SECONDS if details.get("reducingAtSubmission")
                                    else rules.ADDING_POSITION_COOLDOWN_SECONDS)
                self.last_completed_order_cooldowns[rule_key] = cooldown_seconds
                details["completedRecorded"] = True
                if action in {"BUY", "SELL"} and security_type in {"STK", "OPT"}:
                    # IBKR provides the average execution price on a fully filled order.
                    # Prefer it over lastFillPrice for multi-part executions.
                    executed_price = float(avg_fill_price or 0)
                    if not math.isfinite(executed_price) or executed_price <= 0:
                        executed_price = float(last_fill_price or 0)
                    self.cancel_price_overlap_orders_after_fill(
                        order_id, con_id, action, symbol, executed_price
                    )

                if security_type == "STK" and action in {"BUY", "SELL"}:
                    self.reqPositions()

                print(
                    f"Cooldown started: {symbol} {action} order "
                    f"{order_id} was fully filled. "
                    f"New same-side orders are blocked for {cooldown_seconds // 60} minutes. "
                )

        # Confirm the previous loss stop is cancelled before submitting its
        # replacement profit stop. Never have two independent full-size stops.
        if details is not None and details.get("orderRef") == rules.LOSS_STOP_ORDER_REF:
            con_id = details["conId"]
            transition = self.profit_stop_transition.get(con_id)
            if transition is not None and transition["order_id"] == order_id:
                if normalized_status in {"cancelled", "apicancelled"}:
                    self.profit_stop_transition.pop(con_id, None)
                    self.profit_stop_ready.add(con_id)
                    print(f"AUTO_LOSS_STOP {order_id} cancellation confirmed; submitting replacement profit stop.")
                elif normalized_status == "filled":
                    self.profit_stop_transition.pop(con_id, None)
                    self.profit_stop_snapshots.pop(con_id, None)
                    print(f"AUTO_LOSS_STOP {order_id} filled before cancellation; not submitting a competing profit stop.")

        if normalized_status in {
            "cancelled",
            "apicancelled",
            "filled",
        }:
            self.active_sell_orders.pop(order_id, None)
            self.active_stock_orders.pop(order_id, None)
            self.pending_stock_sell_orders.pop(order_id, None)
            self.cancel_requested.discard(order_id)

        # This is intentionally after the old stop is removed from the local
        # active-order cache; send the replacement only on confirmed cancellation.
        if (details is not None and details.get("orderRef") == rules.LOSS_STOP_ORDER_REF
                and normalized_status in {"cancelled", "apicancelled"}):
            con_id = details["conId"]
            if con_id in self.profit_stop_ready:
                saved = getattr(self, "profit_stop_snapshots", {}).pop(con_id, None)
                if saved is not None and abs(self.positions.get(con_id, 0) - saved["qty"]) < 1e-9:
                    self.maybe_place_profit_stop(con_id, saved["symbol"], self.positions[con_id],
                                                 saved["position_value"], saved["unrealized"])

            # Do not retry after an uncertain API submission.

            # Do not retry automatically; avoid duplicate stops after an uncertain API failure.

    def pnlSingle(self, req_id, pos, daily_pnl, unrealized_pnl, realized_pnl, value):
        details = self.pnl_request_symbols.get(req_id)

        if details is None:
            return

        symbol = details["symbol"]
        con_id = details["conId"]
        position_value = float(value)
        unrealized = float(unrealized_pnl)

        if math.isfinite(position_value) and abs(position_value) < 1e100:
            self.position_values[con_id] = position_value
            contract = self.position_contracts.get(con_id)
            if contract is not None and str(contract.secType).upper() == "OPT":
                self.option_market_values[con_id] = position_value

        if math.isfinite(unrealized) and abs(unrealized) < 1e100:
            self.unrealized_pnls[con_id] = unrealized

        if abs(float(pos)) <= 1e-9:
            return

        if not math.isfinite(position_value) or not math.isfinite(unrealized):
            return

        current_position = float(pos)
        self.maybe_place_loss_stop(con_id)
        self.maybe_place_profit_stop(con_id, symbol, current_position, position_value, unrealized)
        contract = self.position_contracts.get(con_id)
        is_stock = contract is not None and str(contract.secType).upper() == "STK"
        is_short = is_stock and current_position < -1e-9
        is_eligible_long = (
            is_stock
            and current_position > 1e-9
            and rules.LOSS_POPUP_LONG_POSITION_VALUE_MIN <= abs(position_value) < rules.LOSS_POPUP_LONG_POSITION_VALUE_MAX
        )
        if (is_short or is_eligible_long) and unrealized <= -rules.LOSS_CUT_AMOUNT:
            now = time.monotonic()
            last_popup = self.last_loss_popup_times.get(con_id, 0.0)

            if now - last_popup >= rules.LOSS_POPUP_INTERVAL_SECONDS:
                self.last_loss_popup_times[con_id] = now
                position_side = "Short" if is_short else "Long"
                print(
                    f"Cut Loss Reminder: {symbol}, "
                    f"Position Value=${abs(position_value):.0f}, "
                    f"Unrealized Loss=${abs(unrealized):.0f}."
                )
                # show_cut_loss_popup(symbol, position_value, unrealized, position_side)

        is_option = contract is not None and str(contract.secType).upper() == "OPT"
        if is_stock:
            if not profit_trigger_reached(unrealized, position_value):
                return
        elif is_option:
            entry_value = abs(position_value - unrealized)
            if (entry_value <= 0 or unrealized <= rules.PROFIT_TRIGGER
                    or unrealized < entry_value * rules.OPTION_PROFIT_TRIGGER_PERCENT):
                return
        else:
            return

        popup_key = con_id if is_option else symbol
        now = time.monotonic()
        last_popup = self.last_profit_popup_times.get(popup_key, 0.0)

        if now - last_popup < rules.PROFIT_POPUP_INTERVAL_SECONDS:
            return

        self.last_profit_popup_times[popup_key] = now
        print(
            f"Take Profit Reminder: {symbol}, "
            f"Position Value=${abs(position_value):.0f}, "
            f"Unrealized Profit=${unrealized:.0f}."
        )
        show_take_profit_popup(symbol, position_value, unrealized)

    def connectionClosed(self):
        print("TWS API connection closed.")

    def error(
        self,
        req_id,
        error_code,
        error_string,
        advanced_order_reject_json="",
    ):
        informational_codes = {
            2104,
            2106,
            2108,
            2150,
            2158,
        }

        if error_code in informational_codes:
            return

        print(
            f"IBKR message: "
            f"request={req_id}, "
            f"code={error_code}, "
            f"message={error_string}"
        )

shutdown_requested = Event()


def confirm_exit(signum, frame):
    if shutdown_requested.is_set():
        return

    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        print("\nCtrl+C detected.", flush=True)
        try:
            confirmation = input('Type "DO NOT TRADE WITH EMOTIONS" to stop the order blocker: ')
        except (EOFError, OSError):
            print("No confirmation received. Risk manager will continue running.", flush=True)
            return

        if confirmation.strip() == "DO NOT TRADE WITH EMOTIONS":
            shutdown_requested.set()
            print("Shutdown confirmed. Stopping risk manager.", flush=True)
        else:
            print("Incorrect phrase. Order blocker will continue running.", flush=True)
    finally:
        signal.signal(signal.SIGINT, confirm_exit)

def api_loop(app: OrderBlocker):
    app.run()

def main():
    if rules.POSITION_SIDE.upper() not in {"LONG", "SHORT", "NONE"}:
        raise ValueError('POSITION_SIDE must be "LONG", "SHORT", or "NONE".')

    app = OrderBlocker()

    print(f"Connecting to TWS at {rules.HOST}:{rules.PORT}...")

    app.connect(
        rules.HOST,
        rules.PORT,
        clientId=rules.CLIENT_ID,
    )

    api_thread = Thread(
        target=api_loop,
        args=(app,),
        daemon=True,
    )

    api_thread.start()

    if not app.ready.wait(timeout=15):
        print("Could not establish a ready connection to TWS.")
        print("Check the TWS API setting and Socket Port.")

        app.disconnect()
        return

    stop_order_reminder_thread = Thread(
        target=app.stop_order_reminder_loop,
        daemon=True,
    )
    stop_order_reminder_thread.start()
    print()
    print("Order blocker is running.")
    print(
        f"Max single order value: ${rules.MAX_SINGLE_TRADE_VALUE:.0f}"
    )
    print(
        f"Max long position value = ${rules.MAX_LONG_POSITION_VALUE:.0f}"
    )
    print(
        f"Max short position value = ${rules.MAX_SHORT_POSITION_VALUE:.0f}"
    )
    print(
        f"Max high beta position value = ${rules.MAX_HIGH_BETA_POSITION_VALUE:.0f}"
    )
    print(
        f"New BUY orders are blocked from "
        f"{rules.BUY_ORDER_BLOCK_START.strftime('%H:%M')} to "
        f"{rules.BUY_ORDER_BLOCK_END.strftime('%H:%M')}."
    )
    print(
        f"Tuesday SELL orders are blocked from "
        f"{rules.TUESDAY_SELL_BLOCK_START.strftime('%H:%M')} to "
        f"{rules.TUESDAY_SELL_BLOCK_END.strftime('%H:%M')}."
    )
    print(
        f"Opening a new short position is blocked from "
        f"{rules.NEW_SHORT_BLOCK_START.strftime('%H:%M')} to "
        f"{rules.NEW_SHORT_BLOCK_END.strftime('%H:%M')}."
    )
    print(
        f"Opening/increasing a position is blocked from "
        f"{rules.AFTERNOON_RULE_START.strftime('%H:%M')} to "
        f"{rules.AFTERNOON_RULE_END.strftime('%H:%M')}."
    )
    print(
        f"5/10 mins cooldown: blocks same-side orders after a fill when reducing/adding to a position."
    )
    print(
        f"Market open direction: {rules.POSITION_SIDE}; "
        f"LONG blocks SELL orders, SHORT blocks BUY orders from "
        f"{rules.MARKET_OPEN_DIRECTION_BLOCK_START.strftime('%H:%M')} to "
        f"{rules.MARKET_OPEN_DIRECTION_BLOCK_END.strftime('%H:%M')}."
    )
    print(
        f"Option restriction: maximum {rules.MAX_OPTION_CONTRACTS_PER_TICKER} contracts per ticker "
        f"across all strikes/expirations."
    )
    print(
        "Option restriction: averaging down a losing option is blocked."
    )
    print(
        "Option restriction: opening short CALL positions is blocked."
    )
    print("Press Control+C to stop.")
    print()

    signal.signal(signal.SIGINT, confirm_exit)

    try:
        while app.isConnected() and not shutdown_requested.is_set():
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nUnexpected keyboard interrupt; disconnecting...")

    finally:
        if app.isConnected():
            app.disconnect()

        print("Order blocker stopped.")

if __name__ == "__main__":
    main()