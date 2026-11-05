#!/usr/bin/env python3
"""Start the backend after loading the project .env without shell evaluation."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def load_project_env(root: Path) -> None:
    from dotenv import load_dotenv

    path = root / '.env'
    if path.exists():
        # Process / systemd environment wins; never evaluate shell syntax or
        # interpolate values, and never print file contents.
        try:
            load_dotenv(path, override=False, interpolate=False)
        except OSError:
            raise RuntimeError('Project environment file is unavailable') from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8787)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    load_project_env(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import uvicorn

    uvicorn.run('server.main:app', host=args.host, port=args.port, reload=False)


if __name__ == '__main__':
    main()
