Build a crypto OHLCV data layer. All tests must run against fixtures/mocks with NO network calls.

The plan must list these files:
- src/data/schema.py (Candle dataclass, ValidationResult dataclass; prices use Decimal, never float)
- src/data/adapters.py (parse_ccxt_ohlcv(rows) -> list[Candle])
- src/data/validation.py (validate_candles(candles, timeframe) -> ValidationResult)
- src/data/store.py (Parquet read/write, partitioned by symbol/timeframe)
- src/data/source.py (DataSource protocol + CCXT adapter)
- tests/data/test_schema.py
- tests/data/test_adapters.py
- tests/data/test_validation.py
- tests/data/test_store.py

validate_candles must enforce, each with its own rejection test:
1. Timestamps strictly increasing, no duplicates.
2. Timestamp spacing exactly equals the timeframe (detect gaps).
3. high >= max(open, close); low <= min(open, close); all prices > 0.
4. volume >= 0.
5. Decimal end-to-end; assert no float leaks.
6. Parquet persistence round-trips losslessly.
7. Pagination stitches pages with no overlap or gap.
8. All timestamps UTC; no naive datetimes.
9. Reject a still-forming final candle (open + timeframe must be strictly in the past).

Also test: malformed API response (missing field, wrong type, empty) raises a clean error, not a crash.
