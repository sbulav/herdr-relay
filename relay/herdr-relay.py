#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14.0", "pywebpush>=2.0.0", "py-vapid>=1.9.0"]
# ///
"""Entry point for the relay — `uv run relay/herdr-relay.py`.

The relay itself is the `herdr_relay` package next to this file. This launcher
exists because PEP 723 inline metadata is only honoured on a single script, so a
package cannot declare its own dependencies for `uv run`. It is also why the name
is hyphenated: `herdr-relay.py` is not an importable module name and can never be
confused with the package it starts.
"""
import asyncio
import os
import sys

# Run from anywhere: the package lives in this file's own directory, which is not
# on sys.path when the script is invoked by absolute path from another cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from herdr_relay import main  # noqa: E402  (path set up above)

if __name__ == "__main__":
    asyncio.run(main())
