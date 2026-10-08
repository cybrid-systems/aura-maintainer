"""Closed-catalog proposer.

The optional MiniMax backend lives in ``llm.py`` and is imported only when
``--proposer llm`` is selected. Importing this package does not load it
and does not read a key file.
"""

from harness.proposers.rules import RulesProposer

__all__ = ["RulesProposer"]
