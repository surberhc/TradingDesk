"""
Strategy engines (SPEC.md §1).

Priority order:
  PRIMARY      regime, duration, defensive
  SUBORDINATE  volatility (a trim within the regime band, never an independent cutter)
  SATELLITE    sector (optional overlay), real_assets (single confirmed-hedge slot)

Build one engine at a time, each with its own passing unit test BEFORE wiring it
into the portfolio (CLAUDE.md hard rule 5).
"""
