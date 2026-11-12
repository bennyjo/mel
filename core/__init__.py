"""Mel's protected engine.

Every module in this package is operator-owned. The allocating agent may not
edit anything under `core/`; `loop.sh` reverts such a change locally and CI
fails any agent commit that touches these paths.
"""
