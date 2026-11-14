#!/usr/bin/env python3
"""Compatibility entry point; prefer ros2 run orbo_runtime base_bench."""
from orbo_runtime.bench.base import main

if __name__ == "__main__":
    raise SystemExit(main())
