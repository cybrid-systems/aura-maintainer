"""Closed catalog of aura-redis choose-fn bodies and rule proposals.

No Aura process and no LLM. Bodies are extracted from the checked-in
choose_*.aura string literals. Parameter edits are three literal
replacements on the normal body.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

LAMBDA_PREFIX = (
    "(lambda (dgets dsets dhits dmisses devicted nkeys dexpired avg_ttl keys_ttl)"
)
DENY_SUBSTRINGS = (
    "ffi",
    "socket",
    "c-func",
    "read-file",
    "write-file",
    "getenv",
    "syscall",
    "plugin",
    ".so",
)
# Heads that appear in the checked-in normal body, plus the review allowlist.
# `lambda` and `else` are in the body; the review list did not spell them,
# but rejecting them would refuse the gold body itself.
CALL_HEADS = frozenset({
    "lambda",
    "let",
    "if",
    "cond",
    "and",
    "or",
    "else",
    "+",
    "*",
    "<",
    ">",
    ">=",
    "quotient",
})

PROFILES = (
    "normal",
    "aggressive",
    "conservative",
    "inverted",
    "broken",
    "nosoft",
    "defensive",
)
ROLES = {
    "normal": "quality",
    "aggressive": "quality",
    "conservative": "quality",
    "nosoft": "control",
    "defensive": "control",
    "inverted": "probe",
    "broken": "negative",
}
QUALITY = ("aggressive", "conservative", "normal")
CONTROLS = ("nosoft", "defensive")

LADDERS: dict[str, tuple[int, ...]] = {
    "min-ops": (120, 80, 40, 20),
    "miss-pin": (55, 30, 15),
    "soft-budget": (25, 20, 15),
}
GOLD = {"min-ops": 40, "miss-pin": 30, "soft-budget": 20}
KNOB_ORDER = ("min-ops", "miss-pin", "soft-budget")
PATTERNS = {
    "min-ops": "(< ops 40)",
    "soft-budget": "(>= erate 20)",
    "miss-pin": "(> miss-pct 30)",
}

HOLD_PARAMS = {"min-ops": 40, "miss-pin": 30, "soft-budget": 20}
RECOVER_PARAMS = {"min-ops": 120, "miss-pin": 55, "soft-budget": 25}


def unescape_aura_string(literal: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(literal):
        ch = literal[i]
        if ch == "\\" and i + 1 < len(literal):
            nxt = literal[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def extract_policy_string(source: str, name: str) -> tuple[str, int]:
    """Return (unescaped body, 1-based line of the define)."""
    marker = f"(define *policy-choose-{name}*"
    idx = source.find(marker)
    if idx < 0:
        raise ValueError(f"missing define *policy-choose-{name}*")
    line = source.count("\n", 0, idx) + 1
    quote = source.find('"', idx)
    if quote < 0:
        raise ValueError(f"missing string for {name}")
    i = quote + 1
    raw: list[str] = []
    while i < len(source):
        ch = source[i]
        if ch == "\\" and i + 1 < len(source):
            raw.append(ch)
            raw.append(source[i + 1])
            i += 2
            continue
        if ch == '"':
            break
        raw.append(ch)
        i += 1
    else:
        raise ValueError(f"unterminated string for {name}")
    return unescape_aura_string("".join(raw)), line


def replace_once(text: str, old: str, new: str) -> str:
    found = text.find(old)
    if found < 0:
        raise ValueError(f"pattern not found: {old}")
    if text.find(old, found + len(old)) >= 0:
        raise ValueError(f"pattern not unique: {old}")
    return text[:found] + new + text[found + len(old):]


def template(normal: str, min_ops: int, miss_pin: int, soft_budget: int) -> str:
    """Three substitutions. Gold arguments reproduce the extracted bytes."""
    text = replace_once(normal, PATTERNS["min-ops"], f"(< ops {min_ops})")
    text = replace_once(text, PATTERNS["soft-budget"], f"(>= erate {soft_budget})")
    text = replace_once(text, PATTERNS["miss-pin"], f"(> miss-pct {miss_pin})")
    return text


def parens_balanced(body: str) -> bool:
    depth = 0
    i = 0
    in_str = False
    while i < len(body):
        ch = body[i]
        if in_str:
            if ch == "\\" and i + 1 < len(body):
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0 and not in_str


def grammar_ok(
    body: str,
    *,
    proposal_id: str | None = None,
    keep_eligible: bool | None = None,
    broken_body: str | None = None,
) -> bool:
    """Closed-catalog tripwire. Not a sandbox.

    probe:broken may skip the 9-arg prefix only when the body is byte-equal
    to the harness-owned broken control and keep_eligible is false.
    """
    if not isinstance(body, str) or "\x00" in body:
        return False
    if not (1 <= len(body) <= 4096):
        return False
    if any(token in body for token in DENY_SUBSTRINGS):
        return False
    if not parens_balanced(body):
        return False
    broken_ok = (
        proposal_id == "probe:broken"
        and keep_eligible is False
        and broken_body is not None
        and body == broken_body
    )
    if broken_ok:
        return True
    return body.startswith(LAMBDA_PREFIX)


def _parse_form(body: str, i: int) -> tuple[object, int]:
    """One s-expression. Binding names stay atoms; calls stay lists."""
    n = len(body)
    while i < n and body[i] in " \t\r\n":
        i += 1
    if i >= n:
        return None, i
    if body[i] == ";":
        while i < n and body[i] != "\n":
            i += 1
        return _parse_form(body, i)
    if body[i] == '"':
        i += 1
        while i < n:
            if body[i] == "\\" and i + 1 < n:
                i += 2
                continue
            if body[i] == '"':
                return "str", i + 1
            i += 1
        return "str", i
    if body[i] == "(":
        i += 1
        items: list[object] = []
        while i < n:
            while i < n and body[i] in " \t\r\n":
                i += 1
            if i < n and body[i] == ";":
                while i < n and body[i] != "\n":
                    i += 1
                continue
            if i >= n:
                break
            if body[i] == ")":
                return items, i + 1
            item, i = _parse_form(body, i)
            items.append(item)
        return items, i
    j = i
    while j < n and body[j] not in " \t\r\n()\":'":
        j += 1
    return body[i:j], j


def _collect_heads(form: object, heads: list[str]) -> None:
    if not isinstance(form, list) or not form:
        return
    head = form[0]
    if not isinstance(head, str) or not head or head[0].isdigit():
        for item in form:
            _collect_heads(item, heads)
        return
    heads.append(head)
    if head == "lambda":
        for item in form[2:]:
            _collect_heads(item, heads)
        return
    if head == "let":
        bindings = form[1] if len(form) > 1 else []
        if isinstance(bindings, list):
            for binding in bindings:
                if isinstance(binding, list) and len(binding) >= 2:
                    _collect_heads(binding[1], heads)
        for item in form[2:]:
            _collect_heads(item, heads)
        return
    if head == "cond":
        for clause in form[1:]:
            _collect_heads(clause, heads)
        return
    for item in form[1:]:
        _collect_heads(item, heads)


def call_heads(body: str) -> list[str]:
    """Call heads only. Lambda parameters and let binding names are not heads."""
    form, _ = _parse_form(body, 0)
    heads: list[str] = []
    _collect_heads(form, heads)
    return heads


def call_heads_allowed(body: str) -> bool:
    return all(head in CALL_HEADS for head in call_heads(body))


def _toward(knob: str, value: int) -> int | None:
    ladder = LADDERS[knob]
    if value not in ladder:
        return None
    index = ladder.index(value)
    gold_at = ladder.index(GOLD[knob])
    if index == gold_at:
        return None
    step = 1 if gold_at > index else -1
    return ladder[index + step]


def _away(knob: str, value: int) -> int | None:
    ladder = LADDERS[knob]
    if value not in ladder:
        return None
    index = ladder.index(value)
    gold_at = ladder.index(GOLD[knob])
    if index == gold_at:
        nxt = gold_at + 1
        return ladder[nxt] if nxt < len(ladder) else None
    step = 1 if gold_at > index else -1
    other = index - step
    if 0 <= other < len(ladder):
        return ladder[other]
    return None


def tried_key(proposal_id: str, body_sha: str, *, once_per_run: bool = False) -> str:
    if once_per_run:
        return f"{proposal_id}|*"
    return f"{proposal_id}|{body_sha}"


@dataclass
class Catalog:
    root: Path
    bodies: dict[str, str]
    lines: dict[str, int]
    normal: str = ""

    def __post_init__(self) -> None:
        self.normal = self.bodies["normal"]


def load_catalog(root: Path | None = None) -> Catalog:
    root = (root or Path(__file__).resolve().parent.parent).resolve()
    policy = root / "target" / "aura-redis" / "src" / "redis" / "policy"
    bodies: dict[str, str] = {}
    lines: dict[str, int] = {}
    for name in PROFILES:
        text = (policy / f"choose_{name}.aura").read_text(encoding="utf-8")
        body, line = extract_policy_string(text, name)
        bodies[name] = body
        lines[name] = line
    return Catalog(root=root, bodies=bodies, lines=lines)


def seed_params(mode: str) -> dict[str, int]:
    if mode == "recover":
        return dict(RECOVER_PARAMS)
    return dict(HOLD_PARAMS)


def seed_body(catalog: Catalog, mode: str) -> str:
    params = seed_params(mode)
    return template(catalog.normal, params["min-ops"], params["miss-pin"], params["soft-budget"])


@dataclass
class Champion:
    family: str
    profile: str
    params: dict[str, int]
    body_sha256: str
    generation: int = 0
    body: str = ""


def _param_id(knob: str, src: int, dst: int) -> str:
    return f"param:{knob}:{src}->{dst}"


def next_proposal(catalog: Catalog, champion: Champion, tried: set[str]) -> dict | None:
    """One next closed-catalog proposal, or None when the queue is empty."""
    sha = champion.body_sha256

    for probe_id, profile in (("probe:inverted", "inverted"), ("probe:broken", "broken")):
        key = tried_key(probe_id, sha, once_per_run=True)
        if key not in tried:
            return {
                "id": probe_id,
                "kind": "probe",
                "outcome_class": "probe",
                "keep_eligible": False,
                "body": catalog.bodies[profile],
                "profile": profile,
            }

    if champion.family == "template":
        for knob in KNOB_ORDER:
            value = int(champion.params[knob])
            toward = _toward(knob, value)
            if toward is not None:
                pid = _param_id(knob, value, toward)
                # The id already names the from/to pair. Keying only by sha
                # would reopen a rolled-back overshoot after a later KEEP.
                if tried_key(pid, sha, once_per_run=True) not in tried:
                    return _param_proposal(catalog, champion, knob, value, toward)
            away_ready = toward is None or tried_key(
                _param_id(knob, value, toward), sha, once_per_run=True
            ) in tried
            away = _away(knob, value) if away_ready else None
            if away is None:
                continue
            pid = _param_id(knob, value, away)
            if tried_key(pid, sha, once_per_run=True) not in tried:
                return _param_proposal(catalog, champion, knob, value, away)

    for profile in CONTROLS:
        pid = f"profile:{profile}"
        if tried_key(pid, sha, once_per_run=True) in tried:
            continue
        return {
            "id": pid,
            "kind": "profile",
            "outcome_class": "rejection",
            "keep_eligible": False,
            "body": catalog.bodies[profile],
            "profile": profile,
        }

    for profile in QUALITY:
        body = catalog.bodies[profile]
        pid = f"profile:{profile}"
        if tried_key(pid, sha, once_per_run=True) in tried:
            continue
        import hashlib
        if hashlib.sha256(body.encode("utf-8")).hexdigest() == sha:
            continue
        return {
            "id": pid,
            "kind": "profile",
            "outcome_class": "improvement",
            "keep_eligible": True,
            "body": body,
            "profile": profile,
        }
    return None


def _param_proposal(catalog: Catalog, champion: Champion, knob: str, src: int, dst: int) -> dict:
    params = dict(champion.params)
    params[knob] = dst
    body = template(catalog.normal, params["min-ops"], params["miss-pin"], params["soft-budget"])
    return {
        "id": _param_id(knob, src, dst),
        "kind": "param",
        "outcome_class": "improvement",
        "keep_eligible": True,
        "knob": knob,
        "from": src,
        "to": dst,
        "body": body,
        "params": params,
    }


def mark_tried(proposal_id: str, body_sha: str) -> str:
    """Once per run. The proposal id already encodes the ladder step."""
    del body_sha
    return tried_key(proposal_id, "", once_per_run=True)


def is_toward_gold(proposal: dict) -> bool:
    if proposal.get("kind") != "param":
        return False
    knob = proposal["knob"]
    src = int(proposal["from"])
    dst = int(proposal["to"])
    return _toward(knob, src) == dst


def simulate_ids(catalog: Catalog, mode: str) -> list[str]:
    """Walk the queue. Toward-gold steps KEEP; everything else leaves the body."""
    import hashlib
    body = seed_body(catalog, mode)
    champion = Champion(
        family="template",
        profile="template",
        params=seed_params(mode),
        body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        body=body,
    )
    tried: set[str] = set()
    ids: list[str] = []
    for _ in range(64):
        proposal = next_proposal(catalog, champion, tried)
        if proposal is None:
            break
        ids.append(proposal["id"])
        tried.add(mark_tried(proposal["id"], champion.body_sha256))
        if is_toward_gold(proposal):
            new_body = proposal["body"]
            champion = Champion(
                family="template",
                profile="template",
                params=dict(proposal["params"]),
                body_sha256=hashlib.sha256(new_body.encode("utf-8")).hexdigest(),
                body=new_body,
                generation=champion.generation + 1,
            )
    return ids
