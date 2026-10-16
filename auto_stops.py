"""Automatic protective stop logic for the IBKR OrderBlocker. """
from copy import copy
import math
from datetime import datetime
from ibapi.order import Order
from trading_rules import (
    TIMEZONE, PROFIT_TRIGGER, PROFIT_TRIGGER_PERCENT, PROFIT_STOP_LOCK,
    PROFIT_STOP_START, PROFIT_STOP_END, PROFIT_STOP_ORDER_REF,
    LOSS_CUT_AMOUNT, LOSS_STOP_PERCENT, LOSS_STOP_START, LOSS_STOP_END, LOSS_STOP_ORDER_REF,
)

def profit_trigger_reached(unrealized, position_value):
    """Shared profit trigger for automatic stock stops and profit reminders."""
    return (math.isfinite(unrealized) and math.isfinite(position_value)
            and abs(position_value) > 1
            and unrealized > PROFIT_TRIGGER
            and unrealized > abs(position_value) * PROFIT_TRIGGER_PERCENT)

class AutoStopsMixin:

    def cancel_closed_position_loss_stop(self, con_id):
        """Cancel only active AUTO_LOSS_STOP orders after their stock position closes."""
        stop_ids = {
            order_id for order_id, pending in self.active_stock_orders.items()
            if pending.get("conId") == con_id
            and pending.get("securityType") == "STK"
            and pending.get("orderRef") == LOSS_STOP_ORDER_REF
        }
        tracked_id = self.automatic_loss_stops.get(con_id)
        if tracked_id is not None and tracked_id in self.active_stock_orders:
            stop_ids.add(tracked_id)
        for order_id in sorted(stop_ids):
            if order_id in self.cancel_requested:
                continue
            self.cancel_requested.add(order_id)
            try:
                self.cancelOrder(order_id)
                print(f"Closed stock position: cancelling AUTO_LOSS_STOP order {order_id}.")
            except Exception as exc:
                self.cancel_requested.discard(order_id)
                print(f"Could not cancel closed-position loss stop {order_id}: {exc}")

    def cancel_closed_position_profit_stop(self, con_id):
        """Cancel only active AUTO_PROFIT_STOP orders after their stock position closes."""
        stop_ids = {
            order_id for order_id, pending in self.active_stock_orders.items()
            if pending.get("conId") == con_id
            and pending.get("securityType") == "STK"
            and pending.get("orderRef") == PROFIT_STOP_ORDER_REF
        }
        tracked_id = self.automatic_profit_stops.get(con_id)
        if tracked_id is not None and tracked_id in self.active_stock_orders:
            stop_ids.add(tracked_id)
        for order_id in sorted(stop_ids):
            if order_id in self.cancel_requested:
                continue
            self.cancel_requested.add(order_id)
            try:
                self.cancelOrder(order_id)
                print(f"Closed stock position: cancelling AUTO_PROFIT_STOP order {order_id}.")
            except Exception as exc:
                self.cancel_requested.discard(order_id)
                print(f"Could not cancel closed-position profit stop {order_id}: {exc}")

    def maybe_place_loss_stop(self, con_id):
        """Place one DAY protective STP for a newly opened stock position."""
        contract = self.position_contracts.get(con_id)
        qty = self.positions.get(con_id, 0.0)
        entry = self.position_avg_costs.get(con_id)
        now = datetime.now(TIMEZONE)
        if (not self.initial_orders_loaded or contract is None
                or str(contract.secType).upper() != "STK"
                or con_id not in self.new_stock_positions
                or con_id in self.automatic_loss_stops
                or con_id in self.profit_stop_ready
                or con_id in self.profit_stop_transition
                or not (LOSS_STOP_START <= now.time() < LOSS_STOP_END)
                or abs(qty) <= 1e-9 or entry is None
                or not math.isfinite(entry) or entry <= 0
                or self.next_order_id is None):
            return
        exit_action = "SELL" if qty > 0 else "BUY"
        # Existing exit orders are never cancelled, replaced, or competed with.
        if any(pending.get("conId") == con_id and pending.get("action") == exit_action
               for pending in self.active_stock_orders.values()):
            return
        max_loss = min(LOSS_CUT_AMOUNT, abs(qty) * entry * LOSS_STOP_PERCENT)
        raw_stop_price = entry - max_loss / qty
        # Round toward the entry price so the stop threshold is never farther away.
        stop_price = (math.ceil(raw_stop_price * 100 - 1e-9) / 100 if qty > 0
                      else math.floor(raw_stop_price * 100 + 1e-9) / 100)
        if not math.isfinite(stop_price) or stop_price <= 0:
            print(f"Skipping invalid loss stop for {contract.symbol}: {stop_price}.")
            return
        order_id = self.next_order_id
        self.next_order_id += 1
        order = Order()
        order.action = exit_action
        order.orderType = "STP"
        order.auxPrice = stop_price
        order.totalQuantity = abs(qty)
        order.tif = "DAY"
        order.outsideRth = False
        order.transmit = True
        order.orderRef = LOSS_STOP_ORDER_REF
        # Deprecated IBKR order attributes must be disabled for API stop orders.
        order.eTradeOnly = False
        order.firmQuoteOnly = False
        account = self.position_accounts.get(con_id)
        if account:
            order.account = account
        self.automatic_loss_stops[con_id] = order_id
        try:
            smart_contract = copy(contract)
            smart_contract.exchange = "SMART"
            self.placeOrder(order_id, smart_contract, order)
            print(f"LOSS STOP: {contract.symbol} {exit_action} {abs(qty):g} STP ${stop_price:.2f}; "
                  f"targeting approximately ${max_loss:.2f} loss "
                  f"(closer of ${LOSS_CUT_AMOUNT} or {LOSS_STOP_PERCENT:.0%} of entry value).")
        except Exception as exc:
            print(f"Could not submit loss stop for {contract.symbol}: {exc}")

    def maybe_place_profit_stop(self, con_id, symbol, qty, position_value, unrealized):
        """Place one protective STP closing order for a newly opened stock position."""
        contract = self.position_contracts.get(con_id)
        now = datetime.now(TIMEZONE)
        if (contract is None or str(contract.secType).upper() != "STK"
                or con_id not in self.new_stock_positions
                or con_id in self.automatic_profit_stops
                or not (PROFIT_STOP_START <= now.time() < PROFIT_STOP_END)
                or abs(qty) <= 1e-9
                or (con_id not in self.profit_stop_ready
                    and not profit_trigger_reached(unrealized, position_value))):
            return

        exit_action = "SELL" if qty > 0 else "BUY"
        if con_id in self.profit_stop_transition:
            return  # Wait for cancellation confirmation before submitting a new stop.

        loss_order_id = self.automatic_loss_stops.get(con_id)
        loss_pending = (self.active_stock_orders.get(loss_order_id)
                        if loss_order_id is not None else None)
        if loss_pending is not None and loss_pending.get("orderRef") == LOSS_STOP_ORDER_REF:
            # Refuse the transition if any independent exit order is already present.
            if any(pending.get("conId") == con_id and pending.get("action") == exit_action
                   for pending_id, pending in self.active_stock_orders.items()
                   if pending_id != loss_order_id):
                return
            # Save the qualifying snapshot before cancellation so the new order
            # does not depend on another market-data update arriving afterward.
            self.profit_stop_snapshots[con_id] = {"symbol": symbol, "qty": qty,
                "position_value": position_value, "unrealized": unrealized}
            self.profit_stop_transition[con_id] = {"order_id": loss_order_id}
            try:
                self.cancelOrder(loss_order_id)
                print(f"Profit threshold reached for {symbol}; requesting cancellation of "
                      f"AUTO_LOSS_STOP {loss_order_id} before replacing it.")
            except Exception as exc:
                self.profit_stop_transition.pop(con_id, None)
                self.profit_stop_snapshots.pop(con_id, None)
                print(f"Could not cancel AUTO_LOSS_STOP {loss_order_id} for {symbol}: {exc}")
            return

        # If a loss stop was placed, require explicit cancellation confirmation.
        # Missing from the local active cache does NOT prove it is cancelled.
        if loss_order_id is not None and con_id not in self.profit_stop_ready:
            return

        # Never compete with any other exit order (including manual take profits).
        if any(pending.get("conId") == con_id and pending.get("action") == exit_action
               for pending in self.active_stock_orders.values()):
            return
        if self.next_order_id is None:
            return

        mark = position_value / qty  # IBKR market value is signed for short positions.
        entry = mark - unrealized / qty
        stop_price = entry + PROFIT_STOP_LOCK / qty
        # Stock stop price should be on the protective side of current price.
        if (not math.isfinite(stop_price) or stop_price <= 0
                or (qty > 0 and stop_price >= mark)
                or (qty < 0 and stop_price <= mark)):
            print(f"Skipping invalid profit stop for {symbol}: mark={mark:.4f}, stop={stop_price:.4f}.")
            return

        # Cent increments are typical for US stocks; other tick sizes need contract-specific rounding.
        stop_price = round(stop_price, 2)
        if (stop_price <= 0 or (qty > 0 and stop_price >= mark)
                or (qty < 0 and stop_price <= mark)):
            return
        order_id = self.next_order_id
        self.next_order_id += 1
        order = Order()
        order.action = exit_action
        order.orderType = "STP"
        order.auxPrice = stop_price
        order.totalQuantity = abs(qty)
        order.tif = "DAY"
        order.outsideRth = False
        order.transmit = True
        order.orderRef = PROFIT_STOP_ORDER_REF
        # Deprecated IBKR order attributes must be disabled for API stop orders.
        order.eTradeOnly = False
        order.firmQuoteOnly = False
        account = self.position_accounts.get(con_id)
        if account:
            order.account = account

        # Reserve before sending, preventing repeated placement from rapid PnL callbacks.
        self.automatic_profit_stops[con_id] = order_id
        self.profit_stop_ready.discard(con_id)
        try:
            smart_contract = copy(contract)
            smart_contract.exchange = "SMART"
            self.placeOrder(order_id, smart_contract, order)
            print(f"PROFIT STOP: {symbol} {exit_action} {abs(qty):g} STP ${stop_price:.2f}; "
                  f"unrealized=${unrealized:.2f}, targeting about ${PROFIT_STOP_LOCK} profit.")
        except Exception as exc:
            print(f"Could not submit profit stop for {symbol}: {exc}")
