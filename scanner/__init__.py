"""stock-async-opp — a local, free, on-demand Indian-market catalyst scanner.

The Python layer is deterministic plumbing only: it fetches, dedupes, stores,
and pre-filters market data into a compact "context pack". It does NOT judge
what is asymmetric — that reasoning is done live by the agent (see CLAUDE.md).
"""

__version__ = "0.1.0"
__app_name__ = "stock-async-opp"
