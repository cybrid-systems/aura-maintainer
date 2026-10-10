"""Optional MiniMax propose backend.

Imported only by ``--proposer llm``. Reads ``~/code/keys/minimax`` at call
time. The key is never written to the environment, the audit, or a log.
A missing key, a timeout, or a non-string reply is ``proposer-unavailable``.
A reply that contains no ``(lambda`` form is ``proposer-no-lambda``.
Neither of those starts Aura.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

MODEL = "MiniMax-M3"
BASE_URL = "https://api.minimaxi.com/v1"
CHAT_URL = BASE_URL + "/chat/completions"
DEFAULT_KEY_FILE = Path.home() / "code" / "keys" / "minimax"
TIMEOUT_SEC = 30.0
# A choose-fn body is at most 4096 bytes. Anything larger is not a proposal.
MAX_RESPONSE_BYTES = 1_000_000

_RETURNS = (
    '""',
    '"lfu|flat|pin"',
    '"lru|flat"',
    '"lru|flat|soft"',
    '"ttl_aware|flat"',
    '"lfu|hot_cold"',
)

_SYSTEM = (
    "Return only one Aura lambda expression for choose-fn. "
    "The parameter list is exactly "
    "(dgets dsets dhits dmisses devicted nkeys dexpired avg_ttl keys_ttl). "
    "Every branch returns one of these strings and nothing else: "
    + ", ".join(_RETURNS)
    + ". No prose, no markdown fence."
)


def user_message(champion_body: str, fixture_rows: list | None = None) -> str:
    """Ask for one edit of the live champion, with the string contract.

    ``fixture_rows`` entries are ``id``, ``args``, ``expect``, and ``got``.
    """
    returns = ", ".join(_RETURNS)
    text = (
        "Edit the current choose-fn. Return one Aura lambda and nothing else. "
        "Every branch must return one of these strings: "
        f"{returns}. Do not return a number.\n"
        "Current body:\n"
        f"{champion_body}"
    )
    if not fixture_rows:
        return text
    matched = [
        row.get("id") for row in fixture_rows if row.get("got") == row.get("expect")
    ]
    missed = [
        row.get("id") for row in fixture_rows if row.get("got") != row.get("expect")
    ]
    lines = [
        "Fixture rows. args are dgets dsets dhits dmisses devicted nkeys dexpired avg_ttl keys_ttl.",
        "A row matches when got equals expect. A tie is not kept.",
        "Leave every row whose got equals expect unchanged.",
        'An expect of "" means that row must return the empty string. Replacing it drops the score.',
    ]
    if matched:
        lines.append("Matching rows: " + ", ".join(str(row_id) for row_id in matched))
    if missed:
        lines.append("Rows to fix: " + ", ".join(str(row_id) for row_id in missed))
    for row in fixture_rows:
        lines.append(
            f"- {row.get('id')} args {row.get('args')} "
            f"expect {json.dumps(row.get('expect', ''))} "
            f"got {json.dumps(row.get('got', '?'))}"
        )
    return text + "\n" + "\n".join(lines)


def key_available(key_file: Path | None = None) -> bool:
    """True when a key file exists and has a non-empty first line."""
    path = key_file or DEFAULT_KEY_FILE
    if not path.is_file():
        return False
    return bool(_read_key(path))


def propose_body(
    *,
    key_file: Path | None = None,
    stub: str | None = None,
    timeout: float = TIMEOUT_SEC,
    champion_body: str = "",
    fixture_rows: list | None = None,
) -> tuple[str | None, str]:
    """Return ``(body, reason)``. ``reason`` is empty when ``body`` is set.

    ``stub`` is a test double. When it is set, the key file is not read and
    the network is not called.
    """
    if stub is not None:
        return _classify(stub)
    path = key_file or DEFAULT_KEY_FILE
    if not path.is_file():
        return None, "proposer-unavailable"
    key = _read_key(path)
    if not key:
        return None, "proposer-unavailable"
    content = _chat(key, timeout, champion_body, fixture_rows)
    if not content:
        return None, "proposer-unavailable"
    return _classify(content)


def _read_key(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    line = raw.strip().splitlines()
    if not line:
        return ""
    return line[0].strip()


def _classify(text: str) -> tuple[str | None, str]:
    body = _extract_lambda(text)
    if body:
        return body, ""
    if text.strip():
        return None, "proposer-no-lambda"
    return None, "proposer-unavailable"


def _chat(
    key: str,
    timeout: float,
    champion_body: str,
    fixture_rows: list | None = None,
) -> str | None:
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_message(champion_body, fixture_rows)},
        ],
        "temperature": 0.2,
        "max_tokens": 2048,
        "thinking": {"type": "disabled"},
    }
    req = urllib.request.Request(
        CHAT_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = _read_limited(resp, MAX_RESPONSE_BYTES)
        if raw is None:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(content, str):
        return None
    return content


def _read_limited(resp, limit: int) -> bytes | None:
    """Read up to ``limit`` bytes. A longer body is refused, not parsed."""
    chunks: list[bytes] = []
    total = 0
    while True:
        block = resp.read(min(65536, limit - total + 1))
        if not block:
            return b"".join(chunks)
        total += len(block)
        if total > limit:
            return None
        chunks.append(block)


def _extract_lambda(text: str) -> str | None:
    start = text.find("(lambda")
    if start < 0:
        return None
    depth = 0
    in_str = False
    i = start
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\" and i + 1 < len(text):
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return None
