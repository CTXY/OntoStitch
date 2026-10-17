"""Check SQL Training resources before a real experiment."""

import argparse
import asyncio
import json
from pathlib import Path

from cortex.bootstrap.preflight import check

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/cortex.yaml"))
    args = parser.parse_args()
    # Avoid printing provider exception payloads, which can include connection details.
    try:
        print(json.dumps(asyncio.run(check(args.config))))
    except Exception as exc:
        raise SystemExit(
            f"Preflight failed ({type(exc).__name__}); inspect local configuration."
        ) from None
