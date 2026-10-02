"""
Signature-contract regression tests — the class of bug the mocks hid.

PRODUCTION INCIDENT 2026-10-02 06:38 UTC: the intrabar resolver called
`fetch_candles_for_symbols(..., ohlcv_out=...)`, but that keyword was missing
from the wrapper's signature. Every resolve failed with

    fetch_candles_for_symbols() got an unexpected keyword argument 'ohlcv_out'
    -> resolved 0 pending, errors=604

The unit tests passed anyway, because they mock `fetch_candles_for_symbols`
wholesale — a mock accepts any keyword. The lesson: when a caller passes a
keyword to a collaborator, assert the COLLABORATOR'S SIGNATURE accepts it,
rather than trusting the call site to be right.

These tests inspect real signatures. They are cheap and they close the whole
class, not just this instance.
"""

from __future__ import annotations

import inspect

import pytest

from regime_platform.sweep_orchestrator import eod_resolver as er
from regime_platform.sweep_orchestrator.price_fetcher import (
    fetch_candles_for_symbols,
    fetch_tradingview_batch,
)


# ─── The incident, reproduced as an assertion ─────────────────────────────


def test_candle_wrapper_accepts_ohlcv_out():
    """The exact keyword the resolver passes must exist on the wrapper."""
    params = inspect.signature(fetch_candles_for_symbols).parameters
    assert "ohlcv_out" in params, (
        "fetch_candles_for_symbols must accept ohlcv_out — the intrabar resolver "
        "passes it. Omitting it fails every resolve in production."
    )


def test_candle_wrapper_forwards_ohlcv_out_to_the_batch_fetcher():
    """Not just accepted — actually forwarded. An accepted-and-dropped keyword
    would silently return closes only, and the intrabar judge would then degrade
    to a close-walk without anyone noticing."""
    src = inspect.getsource(fetch_candles_for_symbols)
    assert "ohlcv_out=ohlcv_out" in src


def test_batch_fetcher_actually_supports_ohlcv_out():
    """The underlying implementation must support it too — the wrapper is only a
    pass-through."""
    params = inspect.signature(fetch_tradingview_batch).parameters
    assert "ohlcv_out" in params


def test_wrapper_and_batch_fetcher_agree_on_the_kwarg_name():
    """Guards a rename on one side only."""
    w = set(inspect.signature(fetch_candles_for_symbols).parameters)
    b = set(inspect.signature(fetch_tradingview_batch).parameters)
    # every keyword the wrapper forwards must exist on the batch fetcher
    for kw in ("ohlcv_out", "include_timestamps"):
        if kw in w:
            assert kw in b, f"wrapper forwards {kw!r} but the batch fetcher lacks it"


# ─── The resolver's own call site ─────────────────────────────────────────


def test_resolver_calls_the_wrapper_with_only_real_keywords():
    """Scan the resolver source for fetch_candles_for_symbols(...) keyword
    arguments and assert each is a real parameter.

    Catches the general case: any future keyword added at a call site that the
    wrapper does not accept. Uses AST so a keyword inside a comment/string is
    not mistaken for a real one.
    """
    import ast

    allowed = set(inspect.signature(fetch_candles_for_symbols).parameters)
    src = inspect.getsource(er)
    tree = ast.parse(src)
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name != "fetch_candles_for_symbols":
            continue
        for kw in node.keywords:
            if kw.arg is None:      # **kwargs splat — cannot check statically
                continue
            assert kw.arg in allowed, (
                f"eod_resolver calls fetch_candles_for_symbols({kw.arg}=...) but "
                f"the wrapper accepts only {sorted(allowed)}"
            )
            checked += 1
    assert checked > 0, "no fetch_candles_for_symbols call sites found to check"


def test_both_resolver_paths_are_wired_for_intrabar():
    """The writing resolver AND the hourly dry run must both branch on the
    basis — wiring only one would let the dry run disagree with what actually
    gets written."""
    src = inspect.getsource(er)
    assert src.count("_resolver_basis()") >= 2
    assert "anchor_post_entry_bars" in src
    assert "_judge_bars" in src
    assert "_resolve_one_bars" in src


# ─── The fallback must exist (it is what limited the incident's blast radius) ──


def test_resolver_has_an_intrabar_fallback_counter():
    """When OHLC is unavailable the resolver falls back to the close-walk rather
    than dropping the signal. That fallback is what turned the 06:38 incident
    from 'wrong basis' into 'loud error' — it must stay."""
    src = inspect.getsource(er)
    assert "intrabar_fallback" in src


def test_fallback_is_counted_in_the_summary():
    """A silent fallback would mix bases invisibly. The counter is the only
    signal an operator gets, so it must be in the returned summary."""
    import ast

    src = inspect.getsource(er.resolve_pending_signals)
    tree = ast.parse(src)
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    assert "intrabar_fallback" in keys, (
        "resolve_pending_signals must initialise intrabar_fallback in its summary"
    )
