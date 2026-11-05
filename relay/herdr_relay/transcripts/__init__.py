"""Structured transcript blocks from the agents' own session stores.

Import the leaf module you need — `transcripts.blocks.pane_blocks` is the entry
point the relay uses; `claude`, `codex` and `opencode` are the backends behind it.
"""
from . import blocks, claude, codex, opencode, refs  # noqa: F401  (import for side-effect-free access)
