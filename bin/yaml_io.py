"""Hand-rolled YAML I/O for cronhub registry.

Avoids pyyaml dependency. Supports:
  - top-level scalars (str, int, bool)
  - list of dicts (the only nested structure we use)
  - scalar lists under a dict key (`tags: [a, b]` or `tags:\n  - a\n  - b`)
  - two list-item indentation styles: 0-space dash (`- id:`) and 2-space dash (`  - id:`)

Defensive: escapes anything YAML could misinterpret; preserves special
characters via quoting. Round-trip safe for our schema.
"""
from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Any

# Characters that force quoting (YAML reserved or whitespace)
_SPECIAL_CHARS = set(":#\"'\n[]{})&*!|>,%@`?,-")

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_identifier(key: str) -> bool:
    """True if `key` could be a real key in this schema.

    Every legitimate registry key is a plain snake_case identifier. Damage
    from a value split on an embedded ": " produces keys containing spaces
    and punctuation, which this rejects.
    """
    return bool(_KEY_RE.match(str(key)))


class RegistryParseError(ValueError):
    """Raised in strict mode when a sub-key cannot be a real key.

    These lines are not skipped by the reader - it MISINTERPRETS them into
    invented keys, which is how the corruption stayed invisible.
    """

    def __init__(self, problems):
        self.problems = list(problems)
        detail = "\n".join("  line %d: %s" % (n, t) for n, t in self.problems[:10])
        super().__init__("registry has %d malformed key line(s):\n%s"
                         % (len(self.problems), detail))


def _emit_scalar(v: Any) -> str:
    # bool MUST be checked before int (bool is a subclass of int)
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        if not v:
            return "[]"
        return "[" + ", ".join(_emit_scalar(x) for x in v) + "]"
    s = str(v)
    if not s:
        return '""'
    # Quote anything that could be misinterpreted as YAML syntax or contains
    # whitespace that would change the parse tree. We over-quote intentionally.
    if any(c in _SPECIAL_CHARS for c in s) or s != s.strip() or s.lower() in {"true", "false", "null", "yes", "no"}:
        # Escape newlines too: an unescaped newline inside a quoted scalar
        # corrupts the document and allows injection of phantom list items.
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'
    return s


def write(path: Path, doc: dict) -> None:
    """Write doc as YAML. Stable ordering: keys are emitted in insertion order.

    Schema: top-level dict of scalars + at most one key whose value is a list
    of dicts (currently `jobs`). Each job may have a `tags` list of scalars.

    Atomic: writes to a sibling tmp file first, then os.replace's into place.
    The tmp must live on the same filesystem as the target, so we use a
    sibling path rather than tempfile.mkstemp (which may pick /tmp on
    cross-filesystem setups).
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        lines: list[str] = []
        for k, v in doc.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                # list-of-dicts key
                lines.append(f"{_emit_scalar(k)}:")
                for item in v:
                    first = True
                    for ik, iv in item.items():
                        prefix = "  - " if first else "    "
                        if isinstance(iv, list):
                            # Scalar-list value: emit inline `[a, b, c]` form.
                            # Reader handles both inline and dash-style; inline
                            # is unambiguous and round-trips cleanly.
                            lines.append(f"{prefix}{_emit_scalar(ik)}: {_emit_scalar(iv)}")
                        else:
                            lines.append(f"{prefix}{_emit_scalar(ik)}: {_emit_scalar(iv)}")
                        first = False
                    lines.append("")
            else:
                lines.append(f"{_emit_scalar(k)}: {_emit_scalar(v)}")
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise


def _write_list_of_dicts(path: Path, key: str, items: list[dict]) -> None:
    """No-op helper preserved for API symmetry; real work in write()."""
    pass


def _parse_scalar(s: str) -> Any:
    s = s.strip()
    if not s:
        return ""
    # YAML single-quoted empty: '' (two single quotes) → ""
    if s == "''" or s == '""':
        return ""
    if s.startswith("'") and s.endswith("'") and len(s) >= 2:
        # YAML single-quoted string — preserve inner content as-is
        return s[1:-1]
    if s.startswith('"') and s.endswith('"'):
        # handle escaped backslash and quote
        body = s[1:-1]
        out = []
        i = 0
        while i < len(body):
            if body[i] == "\\" and i + 1 < len(body):
                nxt = body[i+1]
                out.append("\n" if nxt == "n" else nxt)
                i += 2
            else:
                out.append(body[i])
                i += 1
        return "".join(out)
    if s == "true":
        return True
    if s == "false":
        return False
    if s in ("null", "~"):
        return None
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(x.strip()) for x in _split_list(inner)]
    try:
        return int(s)
    except ValueError:
        return s


def _split_list(s: str) -> list[str]:
    parts, buf, depth, in_str, escape = [], [], 0, False, False
    for c in s:
        if escape:
            buf.append(c); escape = False; continue
        if in_str:
            if c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            buf.append(c)
            continue
        if c == '"':
            in_str = True; buf.append(c)
        elif c in '[{':
            depth += 1; buf.append(c)
        elif c in ']}':
            depth -= 1; buf.append(c)
        elif c == ',' and depth == 0:
            parts.append("".join(buf)); buf = []
        else:
            buf.append(c)
    if buf:
        parts.append("".join(buf))
    return parts


def read(path: Path, strict: bool = False, problems: list | None = None) -> dict:
    """Parse a YAML doc with our extended schema.

    Supports:
      - top-level scalars
      - top-level list of dicts (with two indent styles: 0-space dash, 2-space dash)
      - within a dict, a scalar-list value under any key (`tags:\n  - a\n  - b`)

    Raises RegistryParseError in strict mode if any key name could not be
    real. Pass `problems` to collect them without raising.
    """
    return _read_text(path.read_text(), strict=strict, problems=problems)


def _read_text(text: str, strict: bool = False, problems: list | None = None) -> dict:
    """Parse an already-read document. See read() for the schema.

    State machine:
      - `cur_list`: None or the top-level list we're appending outer items to
      - `cur_item`: None or the current dict-item being built
      - `item_indent`: indent of the dash that started cur_item (-1 if none)
      - `sub_list_key`: None or the key in cur_item that's currently a
        scalar-list whose elements are pending
    """
    if problems is None:
        problems = []
    out: dict = {}
    cur_list = None
    cur_item = None
    item_indent = -1
    sub_list_key = None

    def classify(raw: str) -> tuple[int, str]:
        stripped = raw.lstrip()
        return len(raw) - len(stripped), stripped

    # Track the indent of the parent key whose value is the current sub-list,
    # so we can recognize scalar list elements at parent_indent (original
    # style: `key:\n  - a`) or parent_indent + 2 (standard YAML).
    sub_list_parent_indent = -1

    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        if raw.lstrip().startswith("#"):
            continue

        indent, stripped = classify(raw)

        # Is this line a sub-list scalar? If sub_list_key is active AND the
        # line is a `- ` at any indent ≥ sub_list_parent_indent → sub-list
        # element. (Anything less indented ends the sub-list.)
        is_sub_list_elt = (
            sub_list_key is not None
            and cur_item is not None
            and stripped.startswith("- ")
            and indent >= sub_list_parent_indent
        )

        if is_sub_list_elt:
            _, _, value = stripped.partition("- ")
            cur_item[sub_list_key].append(_parse_scalar(value.strip()))
            continue

        # Anything that wasn't a sub-list element ends the active sub-list.
        if sub_list_key is not None and indent < sub_list_parent_indent:
            sub_list_key = None
            sub_list_parent_indent = -1

        # Step 2: classify current line.
        if stripped.startswith("- ") and indent == 0:
            if cur_list is None:
                continue
            _, _, rest = stripped.partition("- ")
            k, _, v = rest.partition(":")
            parsed_k = _parse_scalar(k.strip())
            if not is_identifier(parsed_k):
                # Same damage as the sub-key branch: this key name cannot be
                # real, it is a value fragment split on an embedded ": ".
                # Record it; still store it so lenient callers see the job.
                problems.append((lineno, raw))
            cur_item = {parsed_k: _parse_scalar(v.strip()) if v.strip() else ""}
            cur_list.append(cur_item)
            item_indent = indent
            sub_list_key = None
            sub_list_parent_indent = -1

        elif stripped.startswith("- ") and indent == 2:
            if cur_list is None:
                continue
            _, _, rest = stripped.partition("- ")
            k, _, v = rest.partition(":")
            parsed_k = _parse_scalar(k.strip())
            if not is_identifier(parsed_k):
                # Same damage as the sub-key branch: this key name cannot be
                # real, it is a value fragment split on an embedded ": ".
                # Record it; still store it so lenient callers see the job.
                problems.append((lineno, raw))
            cur_item = {parsed_k: _parse_scalar(v.strip()) if v.strip() else ""}
            cur_list.append(cur_item)
            item_indent = indent
            sub_list_key = None
            sub_list_parent_indent = -1

        elif indent == 0 and ":" in stripped and not stripped.startswith("-"):
            k, _, v = stripped.partition(":")
            k = k.strip(); v = v.strip()
            if not is_identifier(k):
                # Top-level key name cannot be real: a value fragment that
                # landed at column 0. Record it; still store it so lenient
                # callers keep seeing every top-level key.
                problems.append((lineno, raw))
            if v == "":
                cur_list = []
                out[k] = cur_list
                cur_item = None
                item_indent = -1
                sub_list_key = None
            else:
                out[k] = _parse_scalar(v)
                cur_list = None
                cur_item = None
                item_indent = -1
                sub_list_key = None

        elif indent > item_indent and ":" in stripped and cur_item is not None and not stripped.startswith("-"):
            # Sub-key of current outer item
            k, _, v = stripped.partition(":")
            k = k.strip(); v = v.strip()
            parsed_k = _parse_scalar(k)
            if not is_identifier(parsed_k):
                # This line is shaped like a sub-key but the name cannot be
                # real. It is a value that was split on an embedded ": ".
                # Record it; still store it so lenient callers see the data.
                problems.append((lineno, raw))
            if v == "":
                # Empty value: could be a scalar-list declaration. Initialize as
                # an empty list and let a subsequent dash line activate it.
                cur_item[parsed_k] = []
                sub_list_key = parsed_k
                sub_list_parent_indent = indent
            else:
                cur_item[parsed_k] = _parse_scalar(v)
                sub_list_key = None
                sub_list_parent_indent = -1
        else:
            # A line matching no branch at all. Kept for completeness.
            problems.append((lineno, raw))

    if strict and problems:
        raise RegistryParseError(problems)

    return out


def scan_problems(path: Path) -> list:
    """Return [(lineno, raw_line)] for every line carrying a malformed key.

    Never raises. An unreadable file is itself a problem and is reported as a
    single synthetic entry with line number 0, so an empty result always
    means "read fine, no problems found" and never "could not tell".
    """
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeDecodeError) as exc:
        return [(0, "<unreadable: %s: %s>" % (type(exc).__name__, exc))]
    problems: list = []
    _read_text(text, strict=False, problems=problems)
    return problems
