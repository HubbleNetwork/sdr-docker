"""SDR RX thread -- delegates to the GNU Radio unified backend.

This module exists for backward compatibility; the actual implementation
lives in :mod:`gnuradio_rx`.
"""

from .gnuradio_rx import rx_loop  # noqa: F401
