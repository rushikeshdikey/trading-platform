"""Trading engine — automated order placement, SL management, TSL daemon.

This package is the high-blast-radius part of the codebase. Every Kite
Connect call goes through ``kite_audited`` which logs request, response,
latency, and status to ``broker_audit`` forever. Reads are safe; writes
are gated behind explicit phase flags (see project memory).

Phases:
  - E0  read-only: holdings, positions, profile, reconciliation. NO orders.
  - E1  one-click GTT submit on Auto-Pilot picks. SL+entry placed at Kite.
  - E2  TSL daemon modifies SL per ladder. Auto, hands-off after submit.
  - E3  Auto-submit (no manual confirm). Daily cap, kill switch.
"""
