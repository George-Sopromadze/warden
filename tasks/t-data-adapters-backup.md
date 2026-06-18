Create src/data/adapters.py and tests/data/test_adapters.py. Imports Candle from src/data/schema.py.

The plan must list both files.

src/data/adapters.py exports parse_ccxt_ohlcv(rows: list) -> list[Candle].
Each raw ccxt row is [open_ms, open, high, low, close, volume]. Convert numeric
fields to Decimal (via str to avoid float imprecision). Return a list of Candle.

tests/data/test_adapters.py must verify:
- A valid batch of raw rows parses to Candle objects with Decimal fields.
- Prices are exact Decimal (e.g. parsing "100.10" yields Decimal("100.10"), no float drift).
- Malformed rows raise a clean error, not a crash: a row with a missing field, a row with a wrong type (non-numeric string), and an empty input list.
- Bind one test to the real recorded fixture at tests/data/fixtures/binance_BTC-USDT_1h.json so the parser is tested against the real ccxt row shape.
