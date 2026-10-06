"""Compatibility entry point for the versioned RegressionSpec contract.

The implementation lives in :mod:`regression_gate` because this module is the
runtime qualification gate as well as the frozen specification registry.
"""
from .regression_gate import *  # noqa: F401,F403


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
