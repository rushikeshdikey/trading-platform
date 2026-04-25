"""Chartsmaze-style EOD swing setup scanners.

Three setups, each a pure function on daily bars:
- Horizontal Resistance (stock pressing a cluster of prior highs)
- Trendline Setup (stock near a fitted pivot trendline)
- Tight Setup (low-volatility contraction base)

Data source: NSE bhavcopy (free, one HTTP per day covers all NSE equities),
cached in the ``daily_bars`` table.
"""
