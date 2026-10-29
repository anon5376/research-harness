#!/bin/sh
# Codex cloud environment setup for research-harness: show the toolchain, install nothing.
set -eu
python3 --version
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "research-harness needs Python 3.11 or newer")'
git --version
