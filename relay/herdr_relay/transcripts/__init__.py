"""Structured transcript blocks from the agents' own session stores.

Import the leaf module you need — `transcripts.blocks.pane_blocks` is the entry
point the relay uses; `claude` and `opencode` are the two backends behind it.
"""
from . import blocks, claude, opencode, refs  # noqa: F401  (import for side-effect-free access)
