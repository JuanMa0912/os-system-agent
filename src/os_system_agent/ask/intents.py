"""Versioned catalog of the questions the agent may ask the portal (spec 006 T6).

This file is the security boundary of the whole feature. The model never builds
a URL, never picks a method and never invents a parameter: it can only name an
``id`` that already exists here, and everything else — exact route, method,
typed parameters, fields to strip, answer template, cut-off date field — comes
from the YAML the reviewer read in the pull request diff.

Fail-closed, in the same shape as :mod:`os_system_agent.catalog`: anything
missing, malformed or merely suspicious raises :class:`AskCatalogError` instead
of loading a catalog that is 90% right. In particular:

* an intent without ``drop_fields`` does not load — the portal has no read-only
  role and some responses under an allowed path carry personal data (spec 006
  §7.1), so stripping fields is not optional;
* a route that looks like a prefix, a wildcard, a template or an absolute URL
  does not load — ``/api/margenes/data`` is thirteen different endpoints behind
  one path, and a prefix would grant all thirteen;
* any method other than ``GET`` does not load, and the client checks again
  before it emits a request.

An explicit empty list (``drop_fields: []``, ``parametros: []``) is a decision
the reviewer can see in the diff and is accepted; a missing or null key is
carelessness and is not.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, tzinfo
from enum import StrEnum
from pathlib import Path
from string import Formatter
from typing import Any, NoReturn

import yaml

from os_system_agent.ask.dateparse import DateExpressionError, load_zone
from os_system_agent.redaction import redact

# Only schema version this loader understands. A newer catalog must fail rather
# than be interpreted with the wrong rules.
SUPPORTED_VERSION = 1

# Read-only by construction. The portal's permission model does not distinguish
# reading from writing (spec 006 §3), so the distinction is ours to enforce.
ALLOWED_METHODS: tuple[str, ...] = ("GET",)

# Exact route: /api plus one or more plain segments, no trailing slash, no query
# string, no wildcard, no path template, no host.
_ROUTE_RE = re.compile(r"^/api(?:/[A-Za-z0-9][A-Za-z0-9._-]*)+$")
# Checked before the regex so the error can name the actual problem.
_ROUTE_MARKERS: tuple[tuple[str, str], ...] = (
    ("*", "wildcards are not allowed"),
    ("?", "the route must not carry a query string"),
    ("#", "fragments are not allowed"),
    ("{", "path templates are not allowed"),
    ("}", "path templates are not allowed"),
    ("..", "relative segments are not allowed"),
    ("//", "empty segments are not allowed"),
    ("://", "the route must be a path, not an absolute URL"),
    ("%", "percent-encoding is not allowed in the allowlist"),
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOTTED_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_INTEGER_RE = re.compile(r"^-?\d+$")

# Resolves the catalog timezone. Injectable for the same reason ``collector.py``
# injects its runner: it reaches outside the process (to the host tz database),
# and tests must not depend on that host having one.
ZoneResolver = Callable[[str], tzinfo]

_FORMATTER = Formatter()


class AskCatalogError(RuntimeError):
    """Raised when the intent catalog is missing, malformed, or unsafe to load."""


class ParamType(StrEnum):
    """Declared type of a query parameter (validated before the request is built)."""

    DATE = "fecha"
    INTEGER = "entero"
    TEXT = "texto"
    ENUM = "enum"


@dataclass(frozen=True)
class QueryParam:
    """One query parameter of an intent.

    ``fixed`` pins the value in the catalog: the model cannot supply or change
    it. That is what keeps ``mode=meta`` from becoming ``mode=vendedor`` on the
    same allowed route.
    """

    name: str
    type: ParamType
    required: bool
    fixed: str | None = None
    allowed_values: tuple[str, ...] = ()

    @property
    def model_supplied(self) -> bool:
        """True when the value comes from the question instead of the catalog."""
        return self.fixed is None


@dataclass(frozen=True)
class CutoffField:
    """Where the answer's data cut-off date comes from (spec 006 RF-04).

    ``field`` is a dotted path inside a JSON response. When the intent's own
    response does not carry a cut-off, ``from_intent`` names the intent that
    does, so the answer can still state up to when the data is real.
    """

    field: str
    from_intent: str | None = None


@dataclass(frozen=True)
class AskIntent:
    """A single question the agent is allowed to ask, fully declared."""

    id: str
    description: str
    route: str
    method: str
    params: tuple[QueryParam, ...]
    drop_fields: tuple[str, ...]
    template: str
    cutoff: CutoffField


@dataclass(frozen=True)
class AskCatalog:
    """The validated catalog: the agent's complete question and route allowlist."""

    version: int
    timezone: str
    intents: tuple[AskIntent, ...]

    def get(self, intent_id: str) -> AskIntent:
        """Return the intent with this id, or fail closed if it does not exist."""
        for intent in self.intents:
            if intent.id == intent_id:
                return intent
        raise AskCatalogError(f"unknown intent id: {redact(str(intent_id))[:60]!r}")

    @property
    def routes(self) -> tuple[str, ...]:
        """The route allowlist: every exact path this catalog can reach."""
        return tuple(sorted({intent.route for intent in self.intents}))


def _fail(message: str) -> NoReturn:
    """Raise a domain error whose message carries no secret.

    Catalog errors get logged and forwarded to a chat, and a malformed catalog
    is exactly where a pasted credential ends up, so every message is redacted
    on its way out.
    """
    raise AskCatalogError(redact(message))


def _where(intent_id: str | None, index: int) -> str:
    """Human-readable locator for error messages."""
    return f"intent {intent_id!r}" if intent_id else f"intents[{index}]"


def _require_str(raw: Any, *, where: str, field: str) -> str:
    value = raw.get(field) if isinstance(raw, dict) else None
    if not isinstance(value, str) or not value.strip():
        _fail(f"{where}: '{field}' must be a non-empty string")
    return str(value).strip()


def _validate_route(route: str, *, where: str) -> str:
    """Accept only an exact API path — never a prefix, wildcard, template or URL."""
    for marker, reason in _ROUTE_MARKERS:
        if marker in route:
            _fail(f"{where}: route {route!r} is not exact: {reason}")
    if route.endswith("/"):
        _fail(f"{where}: route {route!r} is not exact: a trailing slash reads as a prefix")
    if not _ROUTE_RE.match(route):
        _fail(
            f"{where}: route {route!r} must be an exact path under '/api/' "
            "(e.g. '/api/portal/freshness')"
        )
    return route


def _validate_method(method: str, *, where: str) -> str:
    normalized = method.upper()
    if normalized not in ALLOWED_METHODS:
        _fail(
            f"{where}: method {method!r} is not allowed; this agent is read-only "
            f"and only {'/'.join(ALLOWED_METHODS)} may be declared"
        )
    return normalized


def _validate_fixed(
    value: str,
    param_type: ParamType,
    values: tuple[str, ...],
    *,
    where: str,
) -> str:
    """Check a catalog-pinned value against its declared type."""
    if param_type is ParamType.DATE:
        try:
            date.fromisoformat(value)
        except ValueError:
            _fail(f"{where}: fixed value {value!r} is not an ISO date (YYYY-MM-DD)")
    elif param_type is ParamType.INTEGER and not _INTEGER_RE.match(value):
        _fail(f"{where}: fixed value {value!r} is not an integer")
    elif param_type is ParamType.ENUM and value not in values:
        _fail(f"{where}: fixed value {value!r} is not one of the declared 'valores'")
    return value


def _parse_param(raw: Any, *, where: str, index: int) -> QueryParam:
    """Validate and build one :class:`QueryParam`."""
    if not isinstance(raw, dict):
        _fail(f"{where}: parametros[{index}] must be a mapping")

    name = _require_str(raw, where=f"{where} parametros[{index}]", field="nombre")
    if not _IDENTIFIER_RE.match(name):
        _fail(f"{where}: parameter name {name!r} must be a plain identifier")

    type_raw = _require_str(raw, where=f"{where} parameter {name!r}", field="tipo")
    try:
        param_type = ParamType(type_raw)
    except ValueError:
        _fail(
            f"{where}: parameter {name!r} has unknown type {type_raw!r}; "
            f"expected one of {', '.join(t.value for t in ParamType)}"
        )

    required = raw.get("obligatorio")
    if not isinstance(required, bool):
        _fail(f"{where}: parameter {name!r} must declare 'obligatorio' as true or false")

    values_raw = raw.get("valores")
    values: tuple[str, ...] = ()
    if param_type is ParamType.ENUM:
        if not isinstance(values_raw, list) or not values_raw:
            _fail(f"{where}: enum parameter {name!r} must declare a non-empty 'valores' list")
        if not all(isinstance(v, str) and v.strip() for v in values_raw):
            _fail(f"{where}: enum parameter {name!r} has a non-string value in 'valores'")
        values = tuple(str(v) for v in values_raw)
        if len(set(values)) != len(values):
            _fail(f"{where}: enum parameter {name!r} repeats a value in 'valores'")
    elif values_raw is not None:
        _fail(f"{where}: parameter {name!r} declares 'valores' but is not of type 'enum'")

    fixed_raw = raw.get("fijo")
    fixed: str | None = None
    if fixed_raw is not None:
        if isinstance(fixed_raw, bool) or not isinstance(fixed_raw, (str, int)):
            _fail(f"{where}: parameter {name!r} has a 'fijo' value that is not text or integer")
        fixed = _validate_fixed(str(fixed_raw), param_type, values, where=f"{where} {name!r}")
        if not bool(required):
            # A pinned value is always sent, so declaring it optional would be a
            # contradiction the request builder could not honour.
            _fail(f"{where}: parameter {name!r} is fixed, so it must be 'obligatorio: true'")

    return QueryParam(
        name=name,
        type=param_type,
        required=bool(required),
        fixed=fixed,
        allowed_values=values,
    )


def _parse_params(raw: Any, *, where: str) -> tuple[QueryParam, ...]:
    """Parse the parameter list; the key is required, an explicit empty list is fine."""
    if "parametros" not in raw:
        _fail(f"{where}: must declare 'parametros' (use an explicit [] when it takes none)")
    params_raw = raw["parametros"]
    if not isinstance(params_raw, list):
        _fail(f"{where}: 'parametros' must be a list")

    params = tuple(
        _parse_param(item, where=where, index=index) for index, item in enumerate(params_raw)
    )
    names = [param.name for param in params]
    if len(set(names)) != len(names):
        _fail(f"{where}: repeated parameter name in 'parametros'")
    return params


def _parse_drop_fields(raw: Any, *, where: str) -> tuple[str, ...]:
    """Parse ``drop_fields`` — declaring it is mandatory (spec 006 §6, plan §3.1)."""
    if "drop_fields" not in raw:
        _fail(
            f"{where}: must declare 'drop_fields'; the portal has no read-only role and "
            "some responses carry personal data, so every intent states what it strips "
            "(use an explicit [] when the response has nothing to strip)"
        )
    fields_raw = raw["drop_fields"]
    if not isinstance(fields_raw, list):
        _fail(f"{where}: 'drop_fields' must be a list (an explicit [] when there is nothing)")
    for item in fields_raw:
        if not isinstance(item, str) or not _IDENTIFIER_RE.match(item.strip()):
            _fail(f"{where}: 'drop_fields' entries must be plain field names")
    fields = tuple(str(item).strip() for item in fields_raw)
    if len(set(fields)) != len(fields):
        _fail(f"{where}: 'drop_fields' repeats a field name")
    return fields


def _validate_template(template: str, *, where: str) -> str:
    """Accept only simple named placeholders in the answer template.

    ``{a.b}`` and ``{a[0]}`` are rejected on purpose: ``str.format`` follows
    attributes and items, which turns a template into a way to walk objects
    reachable from the values (``{x.__class__}``). Names only.
    """
    try:
        parsed = list(_FORMATTER.parse(template))
    except ValueError as exc:
        _fail(f"{where}: 'plantilla' is not a valid format string: {exc}")

    placeholders = 0
    for _literal, field_name, format_spec, _conversion in parsed:
        if field_name is None:
            continue
        placeholders += 1
        if not field_name:
            _fail(f"{where}: 'plantilla' uses a positional placeholder; use named fields")
        if not _IDENTIFIER_RE.match(field_name):
            _fail(
                f"{where}: 'plantilla' placeholder {field_name!r} must be a plain name "
                "(no attribute or index access)"
            )
        if format_spec and "{" in format_spec:
            _fail(f"{where}: 'plantilla' placeholder {field_name!r} uses a nested format spec")
    if placeholders == 0:
        _fail(f"{where}: 'plantilla' has no placeholder, so it could never show a figure")
    return template


def _parse_cutoff(raw: Any, *, where: str) -> CutoffField:
    """Parse ``fecha_corte`` — every answer must be able to state its cut-off date."""
    if "fecha_corte" not in raw:
        _fail(f"{where}: must declare 'fecha_corte'; a figure without its date misleads")
    cutoff_raw = raw["fecha_corte"]
    if not isinstance(cutoff_raw, dict):
        _fail(f"{where}: 'fecha_corte' must be a mapping with a 'campo' key")

    field = _require_str(cutoff_raw, where=f"{where} fecha_corte", field="campo")
    if not _DOTTED_FIELD_RE.match(field):
        _fail(f"{where}: 'fecha_corte.campo' {field!r} must be a field name or dotted path")

    from_intent_raw = cutoff_raw.get("intent")
    if from_intent_raw is None:
        return CutoffField(field=field)
    if not isinstance(from_intent_raw, str) or not from_intent_raw.strip():
        _fail(f"{where}: 'fecha_corte.intent' must be a non-empty intent id when present")
    return CutoffField(field=field, from_intent=str(from_intent_raw).strip())


def _parse_intent(raw: Any, *, index: int) -> AskIntent:
    """Validate and build one :class:`AskIntent` from a raw mapping."""
    if not isinstance(raw, dict):
        _fail(f"intents[{index}] must be a mapping")

    intent_id_raw = raw.get("id")
    intent_id = intent_id_raw.strip() if isinstance(intent_id_raw, str) else None
    where = _where(intent_id, index)
    if not intent_id or not _IDENTIFIER_RE.match(intent_id):
        _fail(f"intents[{index}]: 'id' must be a non-empty identifier")

    description = _require_str(raw, where=where, field="descripcion")
    route = _validate_route(_require_str(raw, where=where, field="ruta"), where=where)
    method = _validate_method(_require_str(raw, where=where, field="metodo"), where=where)
    params = _parse_params(raw, where=where)
    drop_fields = _parse_drop_fields(raw, where=where)
    template = _validate_template(_require_str(raw, where=where, field="plantilla"), where=where)
    cutoff = _parse_cutoff(raw, where=where)

    return AskIntent(
        id=str(intent_id),
        description=description,
        route=route,
        method=method,
        params=params,
        drop_fields=drop_fields,
        template=template,
        cutoff=cutoff,
    )


def _validate_cutoff_references(intents: tuple[AskIntent, ...]) -> None:
    """Every borrowed cut-off must point at an intent that owns one."""
    by_id = {intent.id: intent for intent in intents}
    for intent in intents:
        source_id = intent.cutoff.from_intent
        if source_id is None:
            continue
        if source_id == intent.id:
            _fail(f"intent {intent.id!r}: 'fecha_corte.intent' points at itself")
        source = by_id.get(source_id)
        if source is None:
            _fail(f"intent {intent.id!r}: 'fecha_corte.intent' names unknown intent {source_id!r}")
        elif source.cutoff.from_intent is not None:
            # One hop only: a chain could loop, and a loop means no answer ever
            # learns its cut-off date.
            _fail(
                f"intent {intent.id!r}: 'fecha_corte.intent' {source_id!r} borrows its own "
                "cut-off; the referenced intent must carry the date in its response"
            )


def _log_loaded(catalog: AskCatalog) -> None:
    """One structured, secret-free line: which allowlist this process is running."""
    line = json.dumps(
        {
            "event": "ask_catalog_loaded",
            "version": catalog.version,
            "tz": catalog.timezone,
            "intents": len(catalog.intents),
            "routes": list(catalog.routes),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    print(redact(line), file=sys.stderr, flush=True)


def load_ask_catalog(path: Path, *, resolve_zone: ZoneResolver = load_zone) -> AskCatalog:
    """Load and validate the intent catalog from ``path``.

    Fails closed on: missing file, invalid YAML, unsupported schema version,
    missing/unknown timezone, empty intents list, duplicate ids, a route that is
    not exact, a method other than GET, a malformed parameter, a missing
    ``drop_fields``, an unusable template, or a cut-off that points nowhere.
    """
    path = Path(path)
    if not path.is_file():
        _fail(f"ask catalog file not found: {path}")

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AskCatalogError(redact(f"could not read/parse ask catalog {path}: {exc}")) from exc

    if not isinstance(data, dict):
        _fail(f"ask catalog {path} must be a mapping")

    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        _fail(f"ask catalog {path} must declare an integer 'version'")
    if version != SUPPORTED_VERSION:
        _fail(
            f"ask catalog {path} declares version {version!r}; this build only understands "
            f"version {SUPPORTED_VERSION}"
        )

    timezone = data.get("zona_horaria")
    if not isinstance(timezone, str) or not timezone.strip():
        _fail(
            f"ask catalog {path} must declare 'zona_horaria' (IANA name); the business day "
            "cannot be derived from the host clock"
        )
    timezone = str(timezone).strip()
    try:
        resolve_zone(timezone)
    except DateExpressionError as exc:
        raise AskCatalogError(redact(f"ask catalog {path}: {exc}")) from exc

    intents_raw = data.get("intents")
    if not isinstance(intents_raw, list) or not intents_raw:
        _fail(f"ask catalog {path} has an empty or invalid 'intents' list")

    intents: list[AskIntent] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(intents_raw):
        intent = _parse_intent(raw, index=index)
        if intent.id in seen_ids:
            _fail(f"duplicate intent id in ask catalog: {intent.id!r}")
        seen_ids.add(intent.id)
        intents.append(intent)

    catalog = AskCatalog(
        version=int(version),
        timezone=timezone,
        intents=tuple(intents),
    )
    _validate_cutoff_references(catalog.intents)
    _log_loaded(catalog)
    return catalog
