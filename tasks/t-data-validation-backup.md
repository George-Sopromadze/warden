Create src/data/validation.py and tests/data/test_validation.py. Imports Candle and ValidationResult from src/data/schema.py.

The plan must list both files.

src/data/validation.py exports validate_candles(candles: list[Candle], timeframe: str) -> ValidationResult.
timeframe is a string like "1h", "15m", "1d". Return ValidationResult(ok, violations);
ok is True only if zero violations. Each violated invariant appends a descriptive string.

Enforce these invariants, each with its OWN rejection test in tests/data/test_validation.py:
1. Timestamps strictly increasing, no duplicates.
2. Timestamp spacing exactly equals the timeframe in ms (detect gaps and misalignment).
3. high >= max(open, close); low <= min(open, close); all prices > 0.
4. volume >= 0.
5. Decimal end-to-end; reject any field that is a float, not Decimal.
6. All timestamps represent UTC; reject naive/ambiguous datetimes if any datetime conversion is done.
7. Reject a still-forming final candle: the last candle's open + timeframe must be strictly in the past.

tests/data/test_validation.py must include:
- A fully valid batch returns ok=True, no violations.
- One failing-input test per invariant above (7 rejection tests), each asserting ok=False and the relevant violation is reported.
