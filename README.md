# IBKR Risk Manager

A Python-based trading risk management application for Interactive Brokers Trader Workstation (TWS). It monitors positions and orders, applies predefined trading restrictions, manages protective stock stop orders, and displays alerts to reinforce trading discipline.

## Features

### Order restrictions

- Enforces maximum stock position sizes for long and short positions.
- Applies a separate long-position limit to stocks outside a configurable whitelist.
- Limits options exposure per ticker across strikes and expiration dates.
- Blocks averaging down losing options and opening short call positions.
- Applies configurable trading windows, order cooldowns, and overnight-order limits.
- Checks same-day stock buybacks against shares already sold.
- Monitors account liquidity and restricts exposure increases when liquidity falls below the configured threshold.

### Automatic protective stops

- Places protective stop-loss orders for eligible newly opened stock positions.
- Places profit-protection stops when the configured unrealized-profit thresholds are met.
- Tracks auto-generated stops and cancels applicable outstanding stops when their positions close.

### Alerts and reminders

- Profit-taking and loss-related popup reminders.
- Margin-liquidity warnings and scheduled account-risk reminders.
- Notifications related to particular order restrictions.

## Project files

| File | Purpose |
| --- | --- |
| `risk_manager.py` | Main program, TWS connection, order/position monitoring, and event handling. |
| `trading_rules.py` | Risk limits, trading schedules, and order restriction logic. |
| `auto_stops.py` | Automatic stock stop-loss and profit-protection logic. |
| `popups.py` | User-facing notifications and popup alerts. |
| `start_risk_manager_terminal.sh` | Launches the risk manager in a Terminal window. |
| `install_ibkr_terminal_schedule.sh` | Installs a schedule that checks the application every 10 minutes and can relaunch it if it is not running. |

## Requirements

- Python 3.10 or newer.
- Interactive Brokers Trader Workstation (TWS), with API access enabled.
- Interactive Brokers Python API (`ibapi`).

Install the API package in the Python environment used to launch the application:

```bash
python3 -m pip install ibapi
```

## Running the application

1. Start TWS and sign in.
2. Enable API connections in TWS settings and verify the socket port matches `PORT` in `trading_rules.py`.
3. Open a terminal in the project directory.
4. Start the risk manager:

```bash
python3 risk_manager.py
```

Review the printed messages in the terminal after running the application.

Keep TWS connected while the manager is in use. The application requires an active API connection to monitor orders and enforce its rules.

## Disclaimer

This script is for personal use only.
