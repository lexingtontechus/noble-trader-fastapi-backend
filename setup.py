"""
Setup script for noble-trader-backend.

This file exists primarily for legacy ``pip install -e .`` compatibility.
Modern packaging metadata lives in ``pyproject.toml``.

Usage:
    pip install -e .              # editable install (dev)
    pip install .                 # regular install
    pip install ".[gpu]"          # with GPU HMM extras
    pip install ".[dev]"          # with dev tooling
"""

from setuptools import setup

setup()
