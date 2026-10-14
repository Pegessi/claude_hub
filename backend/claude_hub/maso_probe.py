"""Inspect the local MASO endpoint without starting a service or a model."""

import argparse
import asyncio
import json

from .services.agent_stream.maso_http import MasoServiceUnavailable
from .services.maso_discovery import discover_maso_service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--startup", action="store_true", help="Warn without blocking Hub startup.")
    args = parser.parse_args(argv)
    try:
        base_url, models, _default = asyncio.run(discover_maso_service({}))
    except (ValueError, MasoServiceUnavailable) as exc:
        if args.startup:
            print(f"MASO unavailable: {exc}", flush=True)
            return 0
        print(json.dumps({"available": False, "message": str(exc)}))
        return 1
    if args.startup:
        print(f"MASO ready at {base_url} ({len(models)} models)", flush=True)
    else:
        print(json.dumps({"available": True, "base_url": base_url, "model_count": len(models)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
