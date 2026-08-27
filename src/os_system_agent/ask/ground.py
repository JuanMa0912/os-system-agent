"""Grounding and data-as-data controls for portal answers (spec 006 RF-06, plan §6).

Three independent controls, in the order the answer pipeline applies them:

1. :func:`sanitize_field` — free text coming back from the portal (product,
   supplier or category names) is **data, never instructions**. It is Unicode
   normalized, stripped of control and markup characters, flattened to a single
   line and truncated. It is never concatenated into a system prompt.
2. :func:`render_answer` — the prose is rendered by a **template**, not by a
   model. A model that does not write the answer cannot be injected into it.
   Placeholders are substituted in one single pass, so a value that itself looks
   like ``{placeholder}`` is never expanded.
3. :func:`verify_grounded` — no figure may appear in the prose that is absent
   from the JSON the API returned. Thousands/decimal separators are normalized
   before comparing, so ``1.234.567`` in the prose and ``1234567`` in the JSON
   are the same figure.

Control 3 catches a figure the model *invented*; it does not catch a *malicious*
one, because a hostile string stored in a product name genuinely is in the JSON
(plan §6). That gap is exactly what controls 1 and 2 close.

Fails closed everywhere: a template with an unknown placeholder, a value of an
unexpected type or prose whose figures cannot be parsed raise
:class:`GroundingError` instead of producing a plausible-looking answer.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from os_system_agent.redaction import redact

# Free-text fields are cut here. Long enough for a real product name, short
# enough that a payload cannot push a wall of text into the answer.
MAX_FIELD_LENGTH = 120
TRUNCATION_MARK = "…"

# Rendered when a field has no value. RF-05: a missing datum is said out loud,
# never shown as an empty string or a zero.
SIN_DATO = "sin dato"

# es-CO, which is how the portal shows figures to the same operator: thousands
# with "." and decimals with ",". Grounding normalizes both back before
# comparing, so the display format is a presentation choice, not a contract.
THOUSANDS_SEP = "."
DECIMAL_SEP = ","

# How deep the JSON walk goes before giving up. A pathological payload must not
# turn into a RecursionError halfway through building an answer.
_MAX_DEPTH = 20

# A figure inside prose or inside a JSON string. The look-behind stops "-" from
# being read as a sign when it follows a digit, so "2026-08-24" yields the three
# parts of a date instead of two negative numbers.
_NUMBER_TOKEN = re.compile(r"(?<!\d)-?\d[\d.,]*")

# Template placeholders. A closed, lowercase shape: nothing a data value could
# accidentally look like, and nothing that can carry an expression.
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")

# Characters removed from free text because they build *structure* somewhere
# downstream: markdown code fences and links, HTML tags, template placeholders,
# table cells and escape sequences. Dropping them costs nothing to a reader of a
# product name and takes away the tools an injected string would need.
_STRUCTURAL_CHARS = "`<>{}[]|\\"
_STRUCTURAL_TABLE = {ord(char): " " for char in _STRUCTURAL_CHARS}

# Leading characters that would make a value read as a command or as a new
# block ("/start", "# title", "- item") once it lands in a chat message.
_LEADING_NOISE = "/#*->!.,:;=+ \t"


class GroundingError(RuntimeError):
    """Raised when an answer cannot be rendered or verified safely."""


@dataclass(frozen=True)
class GroundingReport:
    """Result of checking every figure in the prose against the API payload."""

    ok: bool
    #: The prose tokens, verbatim, that have no counterpart in the payload.
    ungrounded: tuple[str, ...] = ()


def _log(event: str, **fields: object) -> None:
    """Emit one structured, secret-free JSON line to stderr."""
    payload = {"component": "ask.ground", "event": event, **fields}
    print(redact(json.dumps(payload, ensure_ascii=False, default=str)), file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Control 1 — free text is data, never instructions
# --------------------------------------------------------------------------- #


def sanitize_field(value: object, *, max_length: int = MAX_FIELD_LENGTH) -> str:
    """Return ``value`` as a single-line, inert data string.

    Applied to every free-text field that comes back from the portal before it
    reaches a template, a log, the state store or a chat message. The steps and
    the reason for each:

    * NFKC normalization — collapses full-width and compatibility look-alikes,
      so an instruction cannot be smuggled past a reader as ``ｉｇｎｏｒａ``.
    * whitespace to spaces, then control/format characters (category ``Cc``/
      ``Cf``) removed — kills newlines, zero-width joiners and bidi overrides,
      which are what let injected text pose as a separate instruction block.
    * structural characters removed — see :data:`_STRUCTURAL_CHARS`.
    * leading command/markup punctuation stripped.
    * truncated to ``max_length`` before anything else consumes it.

    Non-string values are accepted because a JSON field can be null or numeric;
    ``None`` becomes an empty string and other scalars are stringified. The
    result is inert text, not an escaped-and-reversible encoding: it is meant to
    be *read*, and it is the caller's template that presents it as a quoted
    datum.
    """
    if max_length < 1:
        raise GroundingError(f"max_length must be >= 1, got {max_length}")
    if value is None:
        return ""
    if isinstance(value, (Mapping, list, tuple, set)):
        # Un contenedor en un campo de texto libre es una forma inesperada de la
        # respuesta del portal: se rehusa, no se aplana (plan §9).
        raise GroundingError(f"free-text field must be a scalar, got {type(value).__name__}")

    text = value if isinstance(value, str) else str(value)
    text = unicodedata.normalize("NFKC", text)
    text = "".join(" " if char.isspace() else char for char in text)
    text = "".join(char for char in text if unicodedata.category(char) not in ("Cc", "Cf"))
    text = text.translate(_STRUCTURAL_TABLE)
    text = " ".join(text.split())
    text = text.lstrip(_LEADING_NOISE)

    if len(text) > max_length:
        text = text[:max_length].rstrip() + TRUNCATION_MARK
    return text


# --------------------------------------------------------------------------- #
# Control 2 — the prose comes from a template
# --------------------------------------------------------------------------- #


def format_number(value: Decimal | float | int, *, decimals: int | None = None) -> str:
    """Format a figure the way the portal shows it (es-CO separators).

    ``decimals`` defaults to none for whole numbers and two otherwise, which is
    what a report reads best. The rounding this introduces is understood by
    :func:`verify_grounded`, so a displayed ``1.234,57`` still grounds against a
    payload value of ``1234.5678``.
    """
    if isinstance(value, bool):
        raise GroundingError("a boolean is not a figure")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise GroundingError(f"value is not a number: {type(value).__name__}") from exc
    if not number.is_finite():
        raise GroundingError("value is not a finite number")

    if decimals is None:
        decimals = 0 if number == number.to_integral_value() else 2
    if decimals < 0:
        raise GroundingError(f"decimals must be >= 0, got {decimals}")

    grouped = f"{number:,.{decimals}f}"
    # El paso por NUL evita que el segundo reemplazo pise lo que escribio el
    # primero al invertir los separadores del formato ingles.
    return grouped.replace(",", "\x00").replace(".", DECIMAL_SEP).replace("\x00", THOUSANDS_SEP)


def _format_value(name: str, value: object) -> str:
    """Render one placeholder value, failing closed on unexpected shapes."""
    if value is None:
        return SIN_DATO
    if isinstance(value, bool):
        return "sí" if value else "no"
    if isinstance(value, (int, float, Decimal)):
        return format_number(value)
    if isinstance(value, str):
        return sanitize_field(value) or SIN_DATO
    raise GroundingError(f"field {name!r} has an unsupported type: {type(value).__name__}")


def render_answer(template: str, data: Mapping[str, object]) -> str:
    """Render the answer prose from ``template`` and already-filtered ``data``.

    The model never writes this text: it only chose the intent, and the intent
    names the template. Substitution is a **single pass** over the template, so
    a value that contains ``{otro_campo}`` is inserted literally and never
    expanded — that closes the obvious way out of a data slot.

    Fails closed on a placeholder the data does not provide: a report with a
    hole in it is worse than no report.
    """
    if not isinstance(template, str) or not template.strip():
        raise GroundingError("template must be a non-empty string")
    if not isinstance(data, Mapping):
        raise GroundingError(f"data must be a mapping, got {type(data).__name__}")

    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in data:
            missing.append(name)
            return ""
        # Devolver texto desde una funcion de reemplazo lo trata como literal:
        # una barra invertida en el dato no puede convertirse en un escape.
        return _format_value(name, data[name])

    rendered = _PLACEHOLDER.sub(_replace, template)
    if missing:
        raise GroundingError(f"template placeholders missing from data: {sorted(set(missing))}")
    return rendered


# --------------------------------------------------------------------------- #
# Control 3 — every figure in the prose exists in the payload
# --------------------------------------------------------------------------- #


def _to_decimal(text: str) -> Decimal | None:
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _thousands_reading(digits: list[str]) -> str | None:
    """Join ``digits`` as a thousands-grouped integer, or ``None`` if malformed."""
    if not digits or not 1 <= len(digits[0]) <= 3:
        return None
    if any(len(group) != 3 for group in digits[1:]):
        return None
    return "".join(digits)


def number_candidates(token: str) -> frozenset[Decimal]:
    """Every value ``token`` could denote once separators are normalized.

    ``1.234.567`` can only be a thousands-grouped integer; ``45,3`` can only be
    a decimal; ``1.234`` is genuinely ambiguous and yields both readings. Being
    generous here is safe in the direction that matters: grounding asks whether
    the figure *exists* in the payload, and a token with no valid reading at all
    yields an empty set, which counts as ungrounded.
    """
    text = token.strip().rstrip(".,")
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    if not text or not text[0].isdigit():
        return frozenset()

    readings: set[str] = set()
    dots, commas = text.count("."), text.count(",")

    if dots and commas:
        # Con ambos separadores presentes manda el ultimo: ese es el decimal.
        decimal_sep = "." if text.rfind(".") > text.rfind(",") else ","
        thousands_sep = "," if decimal_sep == "." else "."
        head, _, tail = text.rpartition(decimal_sep)
        grouped = _thousands_reading(head.split(thousands_sep))
        if grouped is not None and tail.isdigit():
            readings.add(f"{grouped}.{tail}")
    elif dots or commas:
        separator = "." if dots else ","
        groups = text.split(separator)
        # Nombres propios: en la rama de arriba `head`/`tail` son `str` (vienen de
        # rpartition) y aqui serian `list[str]`/`str`. Reusarlos compila, pero deja
        # una variable con dos tipos segun la rama, que es justo lo que mypy marca
        # y lo que confunde al leer.
        leading_groups, last_group = groups[:-1], groups[-1]
        if len(groups) > 2:
            # Repetido: solo puede ser separador de miles.
            grouped = _thousands_reading(groups)
            if grouped is not None:
                readings.add(grouped)
        else:
            if last_group.isdigit() and leading_groups[0].isdigit():
                readings.add(f"{leading_groups[0]}.{last_group}")
            if len(last_group) == 3:
                grouped = _thousands_reading(groups)
                if grouped is not None:
                    readings.add(grouped)
    elif text.isdigit():
        readings.add(text)

    values = {
        value for value in (_to_decimal(reading) for reading in readings) if value is not None
    }
    if negative:
        values = {-value for value in values}
    return frozenset(values)


def _iter_scalars(data: object, depth: int = 0) -> Iterator[object]:
    """Yield every scalar *value* in a JSON-shaped structure (keys excluded)."""
    if depth > _MAX_DEPTH:
        raise GroundingError(f"payload nested deeper than {_MAX_DEPTH} levels")
    if isinstance(data, Mapping):
        # Solo valores: una clave como "total_2026" no debe fundamentar una cifra.
        for value in data.values():
            yield from _iter_scalars(value, depth + 1)
    elif isinstance(data, (list, tuple, set, frozenset)):
        for item in data:
            yield from _iter_scalars(item, depth + 1)
    else:
        yield data


def payload_values(data: object) -> frozenset[Decimal]:
    """Collect every figure present in an API payload.

    Numbers count as themselves; strings are tokenized the same way the prose
    is, so a date such as ``"2026-08-24"`` or a numeric string ``"1234"`` grounds
    the figures a template renders from it. Booleans are skipped on purpose:
    ``True`` is not the figure ``1``.
    """
    values: set[Decimal] = set()
    for scalar in _iter_scalars(data):
        if scalar is None or isinstance(scalar, bool):
            continue
        if isinstance(scalar, (int, float, Decimal)):
            number = _to_decimal(str(scalar))
            if number is not None:
                values.add(number)
        elif isinstance(scalar, str):
            for token in _NUMBER_TOKEN.findall(scalar):
                values.update(number_candidates(token))
    return frozenset(values)


def _is_present(candidate: Decimal, values: Iterable[Decimal]) -> bool:
    """True when ``candidate`` equals a payload value, exactly or once rounded."""
    exponent = candidate.as_tuple().exponent
    if not isinstance(exponent, int):
        return False
    quantum = Decimal(1).scaleb(exponent)
    for value in values:
        if value == candidate:
            return True
        try:
            # La prosa muestra cifras redondeadas; redondear el dato a los mismos
            # decimales reconoce ese formato sin admitir una cifra inventada.
            if value.quantize(quantum, rounding=ROUND_HALF_UP) == candidate:
                return True
        except InvalidOperation:
            continue
    return False


def verify_grounded(
    prosa: str,
    datos: object,
    *,
    allowed_literals: Iterable[Decimal | float | int | str] = (),
) -> GroundingReport:
    """Check that every figure in ``prosa`` exists in the ``datos`` payload (RF-06).

    ``allowed_literals`` is for figures the template itself writes (a "top 5"),
    which by definition are not in the payload. Keep it small: everything listed
    there is a figure nobody verified.

    Fails closed: prose that is not a string, or a payload of ``None``, raises
    rather than reporting a clean bill of health.
    """
    if not isinstance(prosa, str):
        raise GroundingError(f"prose must be a string, got {type(prosa).__name__}")
    if datos is None:
        raise GroundingError("there is no payload to ground the prose against")

    values = set(payload_values(datos))
    for literal in allowed_literals:
        values.update(number_candidates(str(literal)))

    ungrounded: list[str] = []
    for token in _NUMBER_TOKEN.findall(prosa):
        candidates = number_candidates(token)
        if not candidates or not any(_is_present(c, values) for c in candidates):
            if token not in ungrounded:
                ungrounded.append(token)

    if ungrounded:
        # Solo el conteo: las cifras sin fundamento son texto no verificado y no
        # tienen por que quedar en el log del gateway.
        _log("grounding_rejected", ungrounded=len(ungrounded))
    return GroundingReport(ok=not ungrounded, ungrounded=tuple(ungrounded))


def require_grounded(prosa: str, datos: object) -> str:
    """Return ``prosa`` if every figure is grounded, else raise (plan §9)."""
    report = verify_grounded(prosa, datos)
    if not report.ok:
        raise GroundingError(
            f"{len(report.ungrounded)} figure(s) in the answer are absent from the "
            f"API payload: {', '.join(report.ungrounded)}"
        )
    return prosa


def render_grounded_answer(template: str, data: Mapping[str, object]) -> str:
    """Render the answer and refuse to hand it over unless it is grounded.

    The single entry point the answer pipeline should use: template first, then
    verification, so neither control can be skipped by accident.
    """
    return require_grounded(render_answer(template, data), data)
