"""Model routing by rungs, with a hard cap in dollars (spec 006 RF-02/RF-10, plan §8).

The rungs, cheapest and most predictable first:

===== ============================================ ================
Rung   Resolves                                     Model
===== ============================================ ================
T0     An explicit command (``/p venta ayer``)      none
T1     A phrase that matches catalog keywords       none
T2     An ambiguous phrase                          free tier
T3     What T2 could not classify                   paid
===== ============================================ ================

Two properties hold no matter what a model replies:

* **The enum is closed.** A model returns ``(intent, params)`` and nothing else,
  validated against the intent ids the catalog declares and a strict parameter
  shape. Anything outside is *rejected*, never repaired, never guessed at, and
  never turned into a URL — the router hands back a refusal instead.
* **The cap is in dollars.** A turn counter does not protect against one runaway
  turn, so the worst case of a paid call is reserved against the daily cap
  **before** the call goes out. At the cap the agent refuses and says so; if a
  free rung exists it degrades to it and says that too. It never fails silently.

The local rung (Ollama on the box) is **not available**: verified with
``ollama list`` on every box, nothing is installed. The example config carries it
as ``available: false`` and :func:`load_models_config` refuses to enable it, so
the gap stays visible instead of turning into a mystery timeout.

The model client and the budget ledger are injected, so every path here is
testable without network access or an API key.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

import yaml

from os_system_agent.ask.ground import sanitize_field
from os_system_agent.redaction import redact

#: Rung ids for the two deterministic rungs. They never involve a model, so they
#: are not configurable and a config file may not reuse their ids.
RUNG_COMMAND = "T0"
RUNG_KEYWORDS = "T1"
_RESERVED_RUNG_IDS = frozenset({RUNG_COMMAND, RUNG_KEYWORDS})

#: Prefix of the explicit command form: ``/p <intent> [clave=valor ...]``.
DEFAULT_COMMAND_PREFIX = "/p"

#: A parameter is a closed shape, never free text: the catalog binds it to a
#: query parameter, so anything exotic is refused before it can travel.
_PARAM_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PARAM_VALUE = re.compile(r"^[A-Za-z0-9_:.\-]{1,64}$")
MAX_PARAMS = 8

#: Intent ids and rung ids, also closed shapes.
_INTENT_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RUNG_ID = re.compile(r"^[A-Z][A-Z0-9_]{0,15}$")

_WORD = re.compile(r"[a-z0-9]+")

#: Providers known to be unavailable on every box, with the check that proved it.
_UNAVAILABLE_PROVIDERS = {"ollama_local": "ollama list (ningún box lo tiene instalado)"}

#: Money is kept to the micro-dollar. Never floats: a cap enforced in binary
#: floating point is a cap that drifts.
_MONEY_QUANTUM = Decimal("0.000001")

TIERS = frozenset({"free", "paid"})


class RouterError(RuntimeError):
    """Raised when the model-routing config is missing, malformed, or unsafe."""


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelRung:
    """One configured model rung (immutable, validated at load time)."""

    id: str
    provider: str
    model: str
    tier: str
    #: Worst case reserved against the cap before the call is made.
    max_cost_per_call_usd: Decimal
    cost_per_1k_input_usd: Decimal
    cost_per_1k_output_usd: Decimal

    @property
    def is_free(self) -> bool:
        """True when the rung cannot spend anything (tariffs verified as zero)."""
        return self.tier == "free"


@dataclass(frozen=True)
class ModelsConfig:
    """Daily cap plus the ordered, available model rungs."""

    daily_cap_usd: Decimal
    max_question_chars: int
    rungs: tuple[ModelRung, ...] = ()


def _money(raw: Any, *, where: str, name: str, minimum: Decimal = Decimal(0)) -> Decimal:
    """Coerce a config value to Decimal money or fail closed."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str, Decimal)):
        raise RouterError(f"{where}: '{name}' must be a number, got {type(raw).__name__}")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise RouterError(f"{where}: '{name}' is not a valid amount: {raw!r}") from exc
    if not value.is_finite() or value < minimum:
        raise RouterError(f"{where}: '{name}' must be a finite amount >= {minimum}, got {raw!r}")
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_UP)


def _parse_rung(raw: Any, *, index: int) -> ModelRung | None:
    """Validate one rung. Returns ``None`` when the rung is declared unavailable."""
    where = f"rungs[{index}]"
    if not isinstance(raw, dict):
        raise RouterError(f"{where} must be a mapping, got {type(raw).__name__}")

    rung_id = raw.get("id")
    if not isinstance(rung_id, str) or not _RUNG_ID.match(rung_id):
        raise RouterError(f"{where}: 'id' must match {_RUNG_ID.pattern}, got {rung_id!r}")
    if rung_id in _RESERVED_RUNG_IDS:
        raise RouterError(f"{where}: {rung_id!r} is reserved for the rungs that use no model")

    provider = raw.get("provider")
    model = raw.get("model")
    if not isinstance(provider, str) or not provider.strip():
        raise RouterError(f"{where}: 'provider' must be a non-empty string")
    if not isinstance(model, str) or not model.strip():
        raise RouterError(f"{where}: 'model' must be a non-empty string")

    available = raw.get("available")
    if not isinstance(available, bool):
        # Sin declaracion explicita no se asume disponible: un peldano que
        # "quiza" existe se convierte en un timeout sin explicacion.
        raise RouterError(f"{where}: 'available' must be declared as true or false")

    reason = _UNAVAILABLE_PROVIDERS.get(provider)
    if reason and available:
        raise RouterError(
            f"{where}: provider {provider!r} is not installed on any box ({reason}). "
            f"Keep 'available: false' until it is verified on the box that will use it."
        )
    if not available:
        return None

    tier = raw.get("tier")
    if tier not in TIERS:
        raise RouterError(f"{where}: 'tier' must be one of {sorted(TIERS)}, got {tier!r}")

    max_cost = _money(raw.get("max_cost_per_call_usd"), where=where, name="max_cost_per_call_usd")
    cost_in = _money(raw.get("cost_per_1k_input_usd"), where=where, name="cost_per_1k_input_usd")
    cost_out = _money(raw.get("cost_per_1k_output_usd"), where=where, name="cost_per_1k_output_usd")

    if tier == "free" and (max_cost or cost_in or cost_out):
        raise RouterError(f"{where}: a 'free' rung must declare all its costs as zero")
    if tier == "paid":
        # Un peldano de pago con tarifa cero nunca cargaria al presupuesto: el
        # tope existiria en el fichero y no en la realidad.
        if not max_cost:
            raise RouterError(f"{where}: a 'paid' rung must declare max_cost_per_call_usd > 0")
        if not (cost_in or cost_out):
            raise RouterError(f"{where}: a 'paid' rung must declare a non-zero tariff")

    return ModelRung(
        id=rung_id,
        provider=provider,
        model=model,
        tier=tier,
        max_cost_per_call_usd=max_cost,
        cost_per_1k_input_usd=cost_in,
        cost_per_1k_output_usd=cost_out,
    )


def load_models_config(path: Path) -> ModelsConfig:
    """Load and validate the model-routing config from ``path``.

    Fails closed on: a missing file, invalid YAML, a missing or non-positive
    ``budget.daily_cap_usd``, a missing ``rungs`` key, duplicate rung ids, a paid
    rung placed before a free one (which would spend when it did not have to), a
    mislabelled tier, and any attempt to enable a provider verified as absent.

    An empty (or all-unavailable) ``rungs`` list is legal: it is the safest
    deployment there is — T0 and T1 only, no model in the path at all.
    """
    path = Path(path)
    if not path.is_file():
        raise RouterError(f"models config not found: {path}")

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RouterError(f"could not read/parse models config {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RouterError(f"models config {path} must be a mapping")

    budget = data.get("budget")
    if not isinstance(budget, dict):
        raise RouterError(f"models config {path} must contain a 'budget' mapping")
    cap = _money(budget.get("daily_cap_usd"), where="budget", name="daily_cap_usd")
    if not cap:
        raise RouterError("budget: 'daily_cap_usd' must be greater than zero")

    max_chars = budget.get("max_question_chars", 500)
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise RouterError(f"budget: 'max_question_chars' must be a positive int, got {max_chars!r}")

    if "rungs" not in data:
        raise RouterError(f"models config {path} must contain a 'rungs' list (it may be empty)")
    rungs_raw = data["rungs"]
    if not isinstance(rungs_raw, list):
        raise RouterError(f"models config {path}: 'rungs' must be a list")

    rungs: list[ModelRung] = []
    seen: set[str] = set()
    for index, raw in enumerate(rungs_raw):
        rung = _parse_rung(raw, index=index)
        if rung is None:
            continue
        if rung.id in seen:
            raise RouterError(f"duplicate rung id in models config: {rung.id!r}")
        seen.add(rung.id)
        if rungs and rung.is_free and not rungs[-1].is_free:
            raise RouterError(
                f"rung {rung.id!r}: free rungs must come before paid ones, or the agent "
                f"would pay for what it could have answered for free"
            )
        rungs.append(rung)

    return ModelsConfig(daily_cap_usd=cap, max_question_chars=max_chars, rungs=tuple(rungs))


# --------------------------------------------------------------------------- #
# Injected collaborators
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelReply:
    """What a model rung is allowed to return: an intent, parameters, and usage.

    Nothing else is read. Prose, explanations or tool calls a client might also
    receive are of no interest to the router and never reach the answer.
    """

    intent: str
    params: Mapping[str, str] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0


class ModelClient(Protocol):
    """Classifies a question into ``(intent, params)`` using one rung's model."""

    def classify(self, question: str, rung: ModelRung) -> ModelReply: ...


class BudgetLedger(Protocol):
    """Today's model spend, persisted by the state store (plan §7, ``budget_day``)."""

    def spent_today_usd(self) -> Decimal: ...

    def record_cost_usd(self, amount: Decimal) -> None: ...


@dataclass
class InMemoryBudget:
    """A ledger that forgets on restart. For tests and dry runs only.

    Never use it in the agent: a budget that resets with the process gives a
    crash-loop an unlimited allowance.
    """

    spent: Decimal = Decimal(0)

    def spent_today_usd(self) -> Decimal:
        return self.spent

    def record_cost_usd(self, amount: Decimal) -> None:
        self.spent += amount


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Decision:
    """The outcome of routing one question."""

    rung: str | None
    intent: str | None = None
    params: Mapping[str, str] = field(default_factory=dict)
    used_model: bool = False
    cost_usd: Decimal = Decimal(0)
    refused: bool = False
    #: Operator-facing explanation. Always set when refused or degraded, so a
    #: degradation is never silent.
    message: str | None = None
    #: True when a paid rung was skipped because of the cap but a free rung answered.
    degraded: bool = False
    #: What a model proposed outside the catalog, sanitized, for the intent-miss
    #: backlog (plan §7, ``ask_intent_miss``). Never used to route anything.
    rejected_intent: str | None = None


def _log(event: str, **fields: object) -> None:
    """Emit one structured, secret-free JSON line to stderr."""
    payload = {"component": "ask.router", "event": event, **fields}
    print(redact(json.dumps(payload, ensure_ascii=False, default=str)), file=sys.stderr, flush=True)


def _usd(amount: Decimal) -> str:
    """Format an amount for an operator-facing message (two decimals, rounded up)."""
    return f"{amount.quantize(Decimal('0.01'), rounding=ROUND_UP):.2f}"


def _words(text: str) -> frozenset[str]:
    """Lowercase, accent-folded word set — «margen» and «MARGEN» must match."""
    folded = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(char for char in folded if not unicodedata.combining(char))
    return frozenset(_WORD.findall(stripped))


def _validate_params(raw: Mapping[str, Any]) -> dict[str, str] | str:
    """Return validated params, or a string naming the reason they were refused."""
    if len(raw) > MAX_PARAMS:
        return f"demasiados parámetros ({len(raw)}, máximo {MAX_PARAMS})"
    params: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not _PARAM_KEY.match(key):
            return "un nombre de parámetro no tiene la forma permitida"
        text = value if isinstance(value, str) else str(value)
        if not _PARAM_VALUE.match(text):
            return f"el valor del parámetro {key!r} no tiene la forma permitida"
        params[key] = text
    return params


class AskRouter:
    """Routes a question to an intent, spending as little and as safely as possible.

    ``allowed_intents`` is the closed enum: the ids the intent catalog declares.
    ``keyword_rules`` maps an intent id to the phrases that resolve it without a
    model; every word of a phrase must appear in the question, and a phrase that
    resolves two intents at once is treated as ambiguous rather than guessed.

    ``model_client`` may be ``None``: a deployment with no model configured is
    valid and safe, and simply refuses what T0 and T1 cannot resolve.
    """

    def __init__(
        self,
        *,
        config: ModelsConfig,
        allowed_intents: frozenset[str],
        keyword_rules: Mapping[str, Sequence[str]],
        budget: BudgetLedger,
        model_client: ModelClient | None = None,
        command_prefix: str = DEFAULT_COMMAND_PREFIX,
    ) -> None:
        if not allowed_intents:
            raise RouterError("the intent enum is empty: the router would refuse everything")
        for intent in allowed_intents:
            if not _INTENT_ID.match(intent):
                raise RouterError(f"intent id {intent!r} must match {_INTENT_ID.pattern}")
        unknown = sorted(set(keyword_rules) - allowed_intents)
        if unknown:
            # Una regla que apunta a un intent inexistente jamas dispararia, y
            # el sintoma seria "el agente no entiende", no "hay una errata".
            raise RouterError(f"keyword rules name intents outside the catalog: {unknown}")
        if not command_prefix.startswith("/"):
            raise RouterError(f"command prefix must start with '/', got {command_prefix!r}")

        self._config = config
        self._allowed = allowed_intents
        self._budget = budget
        self._client = model_client
        self._prefix = command_prefix
        self._rules: dict[str, tuple[frozenset[str], ...]] = {
            intent: tuple(_words(phrase) for phrase in phrases if _words(phrase))
            for intent, phrases in keyword_rules.items()
        }

    # -- rungs without a model ---------------------------------------------- #

    def _route_command(self, question: str) -> Decision:
        """T0: an explicit command. Deterministic, instant, no model involved."""
        parts = question.split()
        if len(parts) < 2:
            return self._refuse(
                RUNG_COMMAND,
                f"Uso: `{self._prefix} <intent> [clave=valor ...]`. Falta el intent.",
            )
        intent = parts[1]
        if intent not in self._allowed:
            # Un intent equivocado NO escala a un modelo: el operador escribio un
            # comando explicito, y adivinar lo que quiso decir es justo lo que
            # esta spec evita.
            return self._refuse(
                RUNG_COMMAND,
                f"El intent {intent!r} no está en el catálogo. "
                f"Consulta la lista con el comando de ayuda.",
            )

        raw: dict[str, str] = {}
        for token in parts[2:]:
            key, separator, value = token.partition("=")
            if not separator:
                return self._refuse(
                    RUNG_COMMAND, f"El parámetro {token!r} no tiene la forma clave=valor."
                )
            raw[key] = value

        validated = _validate_params(raw)
        if isinstance(validated, str):
            return self._refuse(RUNG_COMMAND, f"Parámetros rechazados: {validated}.")

        return self._decide(rung=RUNG_COMMAND, intent=intent, params=validated, used_model=False)

    def _route_keywords(self, question: str) -> Decision | None:
        """T1: resolve by keywords. Returns ``None`` when it cannot decide alone."""
        words = _words(question)
        matched = [
            intent
            for intent, phrases in self._rules.items()
            if any(phrase <= words for phrase in phrases)
        ]
        if len(matched) != 1:
            return None
        return self._decide(rung=RUNG_KEYWORDS, intent=matched[0], params={}, used_model=False)

    # -- rungs with a model -------------------------------------------------- #

    def _spent_today(self) -> Decimal | None:
        """Today's spend, or ``None`` if the ledger cannot be read."""
        try:
            return Decimal(str(self._budget.spent_today_usd()))
        except (InvalidOperation, ValueError, TypeError, OSError):
            return None

    def _charge(self, rung: ModelRung, reply: ModelReply | None) -> Decimal:
        """Charge the ledger for one call and return the amount charged.

        A call that raised is charged its worst case: we cannot know what it cost
        upstream, and under-charging a failing call is exactly how a crash-loop
        would spend the whole day's cap without the counter ever noticing.
        """
        if reply is None:
            cost = rung.max_cost_per_call_usd
        else:
            tokens_in = Decimal(max(0, int(reply.input_tokens)))
            tokens_out = Decimal(max(0, int(reply.output_tokens)))
            cost = (tokens_in / 1000) * rung.cost_per_1k_input_usd + (
                tokens_out / 1000
            ) * rung.cost_per_1k_output_usd
        # Hacia arriba: el redondeo nunca debe favorecer al gasto.
        cost = cost.quantize(_MONEY_QUANTUM, rounding=ROUND_UP)
        if cost:
            self._budget.record_cost_usd(cost)
        return cost

    def _route_models(self, question: str) -> Decision:
        """T2 then T3: classify with a model, cheapest rung first."""
        trimmed = question[: self._config.max_question_chars]
        spent_total = Decimal(0)
        blocked_by_budget = False
        called_model = False
        rejected: str | None = None

        for rung in self._config.rungs:
            if not rung.is_free:
                spent = self._spent_today()
                if spent is None or spent + rung.max_cost_per_call_usd > self._config.daily_cap_usd:
                    # Reservar el peor caso ANTES de llamar: un contador de turnos
                    # no protege de un turno desbocado, y un presupuesto ilegible
                    # se trata como agotado.
                    blocked_by_budget = True
                    _log("rung_blocked_by_budget", rung=rung.id)
                    continue

            if self._client is None:
                break

            called_model = True
            try:
                reply = self._client.classify(trimmed, rung)
            except Exception as exc:
                # Un fallo del proveedor no puede tumbar el turno del operador.
                spent_total += self._charge(rung, None)
                # Solo la clase de la excepcion: el mensaje de un cliente HTTP
                # puede llevar la URL con credenciales o la cabecera de auth.
                _log("model_call_failed", rung=rung.id, error=type(exc).__name__)
                continue

            spent_total += self._charge(rung, reply)
            outcome = self._validate_reply(reply)
            if isinstance(outcome, str):
                rejected = sanitize_field(reply.intent, max_length=64)
                _log("intent_rejected", rung=rung.id, reason=outcome)
                continue

            return self._decide(
                rung=rung.id,
                intent=outcome[0],
                params=outcome[1],
                used_model=True,
                cost_usd=spent_total,
                degraded=blocked_by_budget,
                message=(self._budget_message() if blocked_by_budget else None),
                rejected_intent=rejected,
            )

        return self._refuse(
            None,
            self._exhausted_message(blocked_by_budget, rejected),
            cost_usd=spent_total,
            used_model=called_model,
            rejected_intent=rejected,
        )

    def _validate_reply(self, reply: ModelReply) -> tuple[str, dict[str, str]] | str:
        """Validate a model reply against the closed enum, or say why it failed."""
        if not isinstance(reply, ModelReply):
            return "la respuesta del modelo no tiene la forma esperada"
        if not isinstance(reply.intent, str) or reply.intent not in self._allowed:
            # Fuera del enum se RECHAZA. No hay coincidencia aproximada, ni
            # "quiso decir": interpretar la respuesta de un modelo es
            # exactamente el camino que esta spec cierra.
            return "intent fuera del catálogo"
        if not isinstance(reply.params, Mapping):
            return "los parámetros del modelo no son un mapa"
        validated = _validate_params(reply.params)
        if isinstance(validated, str):
            return validated
        return reply.intent, validated

    # -- messages and assembly ---------------------------------------------- #

    def _budget_message(self) -> str:
        spent = self._spent_today()
        spent_text = f"llevo USD {_usd(spent)}" if spent is not None else "no pude leer el gasto"
        return (
            f"Alcancé el tope diario de modelo (USD {_usd(self._config.daily_cap_usd)}, "
            f"{spent_text}), así que no consulté el escalón de pago."
        )

    def _exhausted_message(self, blocked_by_budget: bool, rejected: str | None) -> str:
        if blocked_by_budget:
            return (
                f"{self._budget_message()} No puedo interpretar la pregunta hasta mañana. "
                f"Puedes preguntarla como comando explícito: `{self._prefix} <intent> "
                f"clave=valor`."
            )
        if rejected is not None:
            return (
                "El modelo propuso algo que no está en el catálogo de preguntas, así que lo "
                f"rechacé sin interpretarlo. Queda registrado. Prueba con `{self._prefix} "
                f"<intent> clave=valor`."
            )
        if self._client is None or not self._config.rungs:
            return (
                "No tengo un modelo disponible para interpretar frases libres. "
                f"Usa el comando explícito: `{self._prefix} <intent> clave=valor`."
            )
        return (
            "No pude clasificar la pregunta con ningún escalón disponible. "
            f"Usa el comando explícito: `{self._prefix} <intent> clave=valor`."
        )

    def _decide(
        self,
        *,
        rung: str,
        intent: str,
        params: Mapping[str, str],
        used_model: bool,
        cost_usd: Decimal = Decimal(0),
        degraded: bool = False,
        message: str | None = None,
        rejected_intent: str | None = None,
    ) -> Decision:
        _log(
            "routed",
            rung=rung,
            intent=intent,
            used_model=used_model,
            cost_usd=str(cost_usd),
            degraded=degraded,
        )
        return Decision(
            rung=rung,
            intent=intent,
            params=dict(params),
            used_model=used_model,
            cost_usd=cost_usd,
            message=message,
            degraded=degraded,
            rejected_intent=rejected_intent,
        )

    def _refuse(
        self,
        rung: str | None,
        message: str,
        *,
        cost_usd: Decimal = Decimal(0),
        used_model: bool = False,
        rejected_intent: str | None = None,
    ) -> Decision:
        _log("refused", rung=rung, cost_usd=str(cost_usd), used_model=used_model)
        return Decision(
            rung=rung,
            intent=None,
            params={},
            used_model=used_model,
            cost_usd=cost_usd,
            refused=True,
            message=message,
            rejected_intent=rejected_intent,
        )

    def route(self, question: str) -> Decision:
        """Route ``question`` down the rungs and return what was decided.

        Never raises for a bad question: it returns a refusal with an
        explanation, because an unhandled exception in a chat turn reads to the
        operator exactly like the agent being broken.

        The question is the operator's own text from an allowlisted DM. No portal
        data ever reaches a model through this path — only this string does, and
        only after being trimmed to the configured length.
        """
        if not isinstance(question, str):
            return self._refuse(None, "No recibí texto que pueda enrutar.")
        text = question.strip()
        if not text:
            return self._refuse(None, "La pregunta llegó vacía.")

        if text == self._prefix or text.startswith(f"{self._prefix} "):
            return self._route_command(text)

        by_keywords = self._route_keywords(text)
        if by_keywords is not None:
            return by_keywords

        return self._route_models(text)
