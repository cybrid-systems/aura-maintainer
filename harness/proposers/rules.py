"""Queue the closed choose-fn catalog. No Aura process and no LLM."""

from __future__ import annotations

from harness.catalog import Champion, Catalog, next_proposal


class RulesProposer:
    name = "rules"

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def propose(self, champion: Champion, tried: set[str]) -> dict | None:
        return next_proposal(self.catalog, champion, tried)
