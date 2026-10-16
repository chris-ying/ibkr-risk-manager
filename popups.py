"""Non-blocking macOS alert functions for the IBKR order blocker."""

import subprocess
from threading import Thread


def show_stop_order_reminder_popup():
    """Show a non-blocking macOS reminder to place stop orders before market open."""
    def popup():
        try:
            apple_script = """
            display alert "PLACE STOP ORDERS" ¬
                as warning ¬
                buttons {"OK"} ¬
                default button "OK"
            """

            subprocess.run(
                ["osascript", "-e", apple_script],
                check=False,
                stdout=subprocess.DEVNULL,
            )
        except Exception as exc:
            print(f"Could not show stop-order reminder popup: {exc}")

    Thread(target=popup, daemon=True).start()

def show_take_profit_popup(symbol, position_value, unrealized_pnl):
    """Show a non-blocking native macOS warning for a profitable position."""
    def popup():
        try:
            message = (
                f"{symbol}\n\n"
                f"Position value: ${abs(position_value):,.0f}\n"
                f"Unrealized profit: ${unrealized_pnl:,.0f}"
            )

            apple_script = """
            on run argv
                display alert "LOCK IN $100 PROFITS" ¬
                    message (item 1 of argv) ¬
                    as warning ¬
                    buttons {"OK"} ¬
                    default button "OK"
            end run
            """

            subprocess.run(
                ["osascript", "-e", apple_script, message],
                check=False,
                stdout=subprocess.DEVNULL,
            )
        except Exception as exc:
            print(f"Could not show take-profit popup: {exc}")

    Thread(target=popup, daemon=True).start()


def show_cut_loss_popup(symbol, position_value, unrealized_pnl, position_side):
    """Show a non-blocking native macOS warning for a losing long or short position."""
    def popup():
        try:
            message = (
                f"{symbol}\n\n"
                f"{position_side} position value: ${abs(position_value):.0f}\n"
                f"Cut loss: ${abs(unrealized_pnl):.0f}"
            )

            apple_script = """
            on run argv
                display alert "CUT LOSS" ¬
                    message (item 1 of argv) ¬
                    as warning ¬
                    buttons {"OK"} ¬
                    default button "OK"
            end run
            """

            subprocess.run(
                ["osascript", "-e", apple_script, message],
                check=False,
                stdout=subprocess.DEVNULL,
            )
        except Exception as exc:
            print(f"Could not show loss popup: {exc}")

    Thread(target=popup, daemon=True).start()


def show_excess_sell_orders_popup(symbol, held_shares, pending_shares, order_count):
    """Warn about aggregate open stock SELL exposure without changing orders."""
    def popup():
        try:
            excess = pending_shares - held_shares
            message = (
                f"{symbol}\n\n"
                f"Shares held: {held_shares:g}\n"
                f"Total pending SELL: {pending_shares:g}\n"
                f"Excess SELL shares: {excess:g}\n\n"
                "SELL ORDERS EXCEED YOUR HOLDINGS. REVIEW YOUR ORDERS."
            )
            script = """
            on run argv
                display alert "EXCESS SELL ORDERS" ¬
                    message (item 1 of argv) ¬
                    as warning ¬
                    buttons {"OK"} ¬
                    default button "OK"
            end run
            """
            subprocess.run(["osascript", "-e", script, message], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            print(f"Could not show excess SELL orders popup: {exc}")

    Thread(target=popup, daemon=True).start()


def format_top_holdings(holdings, currency):
    """Only ticker and absolute market value, in descending order."""
    if not holdings:
        return ""
    lines = [f"{symbol}: {value:.0f} {currency}" for symbol, value in holdings]
    return "\nTop 3 holdings:\n" + "\n".join(lines) + "\n"


def show_trim_position_popup(account, excess_liquidity, net_liquidation, currency, holdings=()):
    """Display a non-blocking macOS warning; no orders are placed."""
    def popup():
        try:
            ratio = excess_liquidity / net_liquidation * 100
            message = (
                f"Account: {account}\n"
                f"Net Liquidation: {net_liquidation:.0f} {currency}\n"
                f"Excess Liquidity: {excess_liquidity:.0f} {currency}\n"
                f"Cash Level: {ratio:.0f}%\n"
                f"{format_top_holdings(holdings, currency)}\n"
                "TRIM YOUR POSITIONS TO RESTORE LIQUIDITY."
            )
            script = """
            on run argv
                display alert "LOW EXCESS LIQUIDITY — TRIM POSITIONS" ¬
                    message (item 1 of argv) ¬
                    as warning ¬
                    buttons {"OK"} ¬
                    default button "OK"
            end run
            """
            subprocess.run(["osascript", "-e", script, message], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            print(f"Could not show excess-liquidity popup: {exc}")

    Thread(target=popup, daemon=True).start()


def show_margin_30_popup(account, excess_liquidity, net_liquidation, currency, holdings=()):
    """Non-blocking macOS warning when the account liquidity ratio is below 30%."""
    def popup():
        try:
            ratio = excess_liquidity / net_liquidation * 100
            message = (
                f"Account: {account}\n"
                f"Net Liquidation: {net_liquidation:.0f} {currency}\n"
                f"Excess Liquidity: {excess_liquidity:.0f} {currency}\n"
                f"Cash Level: {ratio:.0f}%\n"
                f"{format_top_holdings(holdings, currency)}\n"
                "TRIM YOUR POSITIONS TO RESTORE LIQUIDITY."
            )
            script = """
            on run argv
                display alert "MARGIN WARNING — LIQUIDITY BELOW 30%" ¬
                    message (item 1 of argv) ¬
                    as warning ¬
                    buttons {"OK"} ¬
                    default button "OK"
            end run
            """
            subprocess.run(["osascript", "-e", script, message], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            print(f"Could not show 30% margin popup: {exc}")

    Thread(target=popup, daemon=True).start()
