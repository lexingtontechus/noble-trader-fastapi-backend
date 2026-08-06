"""
Multivariate Gaussian HMM — replaces the discrete MarkovChain as the p_markov source.

Public API:
  • FeatureBuffer  — in-memory rolling per-symbol feature store
  • HmmModel       — 5-state Gaussian HMM with 3-state emit contract
  • quote_fetcher  — TVDA /api/quote/batch fetcher (⌈N/10⌉ chunked POSTs;
                     full batch consolidation 2026-08-05)
  • ta_fetcher     — computes 5 TA indicators LOCALLY from the sweep's
                     batch OHLCV (no per-symbol HTTP — TVDA has no batch
                     TA endpoint)
  • latest_ta      — extract the 5 TA values from a buffer for persistence

See TALARIA-MULTIVARIATE-HMM-PROPOSAL.md for the full design.
"""

from regime_platform.services.hmm.feature_buffer import (
    FEATURE_NAMES,
    N_FEATURES,
    FeatureBuffer,
)
from regime_platform.services.hmm.model import (
    HmmModel,
    N_STATES,
    STATE_LABELS,
    STATES_3,
)
from regime_platform.services.hmm.ta_extract import latest_ta

__all__ = [
    "FeatureBuffer",
    "FEATURE_NAMES",
    "N_FEATURES",
    "HmmModel",
    "N_STATES",
    "STATE_LABELS",
    "STATES_3",
    "latest_ta",
]
