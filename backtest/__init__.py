"""Offline, event-driven backtesting utilities.

The package deliberately has no dependency on the GM SDK.  Data acquisition is
handled by :mod:`futures_trend_quant.data.gm_export`; replay and matching remain
deterministic and can therefore be tested without a broker connection.
"""

from .models import FeeSchedule, Fill, Position, SignalIntent, Trade

__all__ = ["FeeSchedule", "Fill", "Position", "SignalIntent", "Trade"]
