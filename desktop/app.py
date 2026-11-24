"""Native window for the standard-library Research Harness server."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rh import config, serve


class Api:
    """What the page may ask of the window: the composer's Choose folder… opens macOS's own folder dialog and gets
    the picked path back ("" when cancelled). The server still decides whether it is a git working tree."""

    def __init__(self, webview):
        self._webview, self._window = webview, None  # private: pywebview hands public attributes to the page

    def choose_folder(self):
        kinds = getattr(self._webview, "FileDialog", None)  # pywebview 5 names it FileDialog.FOLDER; 4, FOLDER_DIALOG
        kind = kinds.FOLDER if kinds is not None else self._webview.FOLDER_DIALOG
        picked = self._window.create_file_dialog(kind) if self._window is not None else None
        return str(picked[0]) if picked else ""


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
            origin = urllib.parse.urlsplit(url)
            no_token_url = urllib.parse.urlunsplit((origin.scheme, origin.netloc, "/", "", ""))
            statuses = []
            for target in (url, no_token_url):
                try:
                    with urllib.request.urlopen(target, timeout=10) as response:
                        statuses.append(response.status)
                except urllib.error.HTTPError as error:
                    statuses.append(error.code)
                    error.close()
                except OSError as error:
                    statuses.append(type(error).__name__)
            ok = statuses == [200, 403]
            print(f"smoke {'ok' if ok else 'failed'} {statuses[0]} {statuses[1]}")
            return 0 if ok else 1

        import webview

        api = Api(webview)
        api._window = webview.create_window("Research Harness", url, width=1280, height=800,
                                           min_size=(1024, 640), js_api=api)
        webview.start()
        return 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    raise SystemExit(main())
