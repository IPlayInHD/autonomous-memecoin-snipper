"""Alias so `python -m sniper.evaluate` runs the evaluation protocol."""

from .evaluation.protocol import main

if __name__ == "__main__":
    raise SystemExit(main())
