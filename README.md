# Strength vs BTC — daily Telegram report

Once a day at **08:05 Manila** (right after the daily close) GitHub runs `strength_report.py`:

1. Downloads closed daily candles for BTC and every coin (Binance; `MEXC:XEL` style for other exchanges)
2. Builds each ALT/BTC ratio and scores it exactly like the TradingView table
   (1D / 7D %, EMA50, BMSB, internal + swing structure, score −4..+4)
3. Sends the report to Telegram
4. Appends the day's results to `data/strength_log.csv` (the journal for edge testing later)

## Files
```
strength_report.py
requirements.txt
.github/workflows/strength_report.yml
```

## Setup (phone friendly)
1. **New repo** on GitHub (private is fine), e.g. `STRENGTH-VS-BTC`.
2. **Add the 3 files** with *Add file → Create new file*. For the workflow, type the full name
   `.github/workflows/strength_report.yml` — the slashes create the folders.
3. **Secrets:** repo *Settings → Secrets and variables → Actions → New repository secret*
   - `TELEGRAM_BOT_TOKEN` — your bot token from @BotFather
   - `TELEGRAM_CHAT_ID` — your chat id
4. **Test:** *Actions* tab → *Strength vs BTC daily report* → *Run workflow*.
   The message should arrive within a minute, and `data/strength_log.csv` appears in the repo.

## Change coins / settings
Edit the top of `strength_report.py` (`COINS`, `EMA_LEN`, `BMSB_TF`, …). Keep them the same as the
TradingView indicator so the report and the table agree.

## Notes
- Closes, % changes, EMA and BMSB match TradingView exactly. Ratio highs/lows are approximated from
  the two charts, so a structure break can occasionally differ by a day from the TradingView table.
- GitHub may start scheduled runs a few minutes late; that's normal.
- GitHub pauses schedules in repos with no activity for 60 days — the daily journal commit keeps it active.
