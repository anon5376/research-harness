"""Native window for the standard-library Research Harness server."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rh import config, serve


def main(argv: list[str] | None = None) -> int:
    """Run the local board, or check its HTTP endpoint without importing webview."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args(argv)
    server, url = serve.make_server(config.load(), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if args.smoke:
            with urllib.request.urlopen(url, timeout=10) as response:
                status = response.status
            print(f"smoke ok {status}")
            return 0 if status == 200 else 1

        import webview

        webview.create_window("Research Harness", url, width=1100, height=760,
                              min_size=(375, 600))
        webview.start()
        return 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    raise SystemExit(main())
