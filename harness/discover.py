"""Static discover. Reads the target tree and does not start Aura."""

from __future__ import annotations

from pathlib import Path

from harness.catalog import (
    GOLD,
    PATTERNS,
    PROFILES,
    ROLES,
    Catalog,
    load_catalog,
)


def _issue(
    issue_id: str,
    kind: str,
    path: str,
    line: int,
    summary: str,
    *,
    propose: bool,
    **extra: object,
) -> dict:
    row = {
        "id": issue_id,
        "kind": kind,
        "path": path,
        "line": line,
        "summary": summary,
        "propose": propose,
    }
    row.update(extra)
    return row


def discover(root: Path | None = None, catalog: Catalog | None = None) -> list[dict]:
    catalog = catalog or load_catalog(root)
    root = catalog.root
    policy = root / "target" / "aura-redis" / "src" / "redis" / "policy"
    issues: list[dict] = []

    for name in PROFILES:
        path = policy / f"choose_{name}.aura"
        rel = str(path.relative_to(root))
        role = ROLES[name]
        issues.append(_issue(
            f"catalog:{name}",
            "catalog",
            rel,
            catalog.lines[name],
            f"role={role}",
            propose=role == "quality",
            role=role,
        ))

    normal_rel = str((policy / "choose_normal.aura").relative_to(root))
    normal_src = (policy / "choose_normal.aura").read_text(encoding="utf-8")
    for knob, pattern in PATTERNS.items():
        at = normal_src.find(pattern)
        line = normal_src.count("\n", 0, at) + 1 if at >= 0 else catalog.lines["normal"]
        issues.append(_issue(
            f"threshold:{knob}",
            "threshold",
            normal_rel,
            line,
            f"{knob} gold {GOLD[knob]}",
            propose=True,
            gold=GOLD[knob],
            pattern=pattern,
        ))

    for name in ("nosoft", "inverted", "broken"):
        body = catalog.bodies[name]
        absent = PATTERNS["soft-budget"] not in body and "(>= erate" not in body
        if absent:
            issues.append(_issue(
                f"threshold:soft-budget:absent:{name}",
                "threshold",
                str((policy / f"choose_{name}.aura").relative_to(root)),
                catalog.lines[name],
                f"{name} has no soft-budget gate",
                propose=False,
                absent=True,
                profile=name,
            ))

    test_path = root / "target" / "aura-redis" / "tests" / "test_hot_strategy_policy.aura"
    test_src = test_path.read_text(encoding="utf-8")
    drifted = (
        "(choose-fn 10 100 5 5)" in test_src
        and 'string=? w "lfu"' in test_src
        and 'string=? r1 "lru"' in test_src
        and LAMBDA_in_normal(catalog)
    )
    if drifted:
        issues.append(_issue(
            "contract-drift:test_hot_strategy_policy",
            "drift",
            str(test_path.relative_to(root)),
            1,
            "4-arg lfu/lru smoke does not match the 9-arg normal body",
            propose=False,
        ))

    if "noop|flat" in normal_src and "noop|flat" not in catalog.normal and "(>= erate 50)" not in catalog.normal:
        issues.append(_issue(
            "comment-drift:choose_normal:erate-50",
            "drift",
            normal_rel,
            1,
            "comment claims erate >= 50 returns noop|flat; the cond has no such branch",
            propose=False,
        ))

    readme = (policy / "README.md").read_text(encoding="utf-8")
    workloads = (root / "target" / "aura-redis" / "docs" / "workloads.md").read_text(encoding="utf-8")
    if ("lfu|hot_cold" in readme or "lfu|hot_cold" in workloads) and "lfu|hot_cold" not in catalog.normal:
        issues.append(_issue(
            "doc-drift:policy-readme-hot-cold",
            "drift",
            str((policy / "README.md").relative_to(root)),
            1,
            "docs still say lfu|hot_cold; choose_normal writes lfu|flat",
            propose=False,
        ))
    return issues


def LAMBDA_in_normal(catalog: Catalog) -> bool:
    return "devicted nkeys dexpired avg_ttl keys_ttl" in catalog.normal


def proposals_from(issues: list[dict]) -> list[dict]:
    return [row for row in issues if row.get("propose") is True]
