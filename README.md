# Kalshi Line Keeper

This repository holds a $20-capped Kalshi liquidity-incentive runner. It is dry-run by default.

The runner is not configured or authorized to place live orders yet. Before using it, select an exact active rewarded market, verify its contract and incentive terms, and add Kalshi API credentials as GitHub Actions secrets. Keep `KALSHI_LIVE` unset until you have inspected dry-run logs. A filled order creates a position that this program does not close automatically.

See the full setup guide in the uploaded package files.
