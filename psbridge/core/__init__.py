"""Safety-critical high-level operations built on top of a SimBackend.

The core layer owns the invariants that make experiments reproducible:
verified writes, no silent object overwrite, isolated model clones, and
timeout-bounded operations.
"""
