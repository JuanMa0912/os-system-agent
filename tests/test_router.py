"""Tests for the model router (spec 006 RF-02/RF-07, plan §8).

Two properties carry the weight here.

**The cheap rungs must not touch a model.** The parked ``/estado`` failed because
a free model was in the loop for something deterministic, and it hung, asked for
parameters or ran away. So T0 and T1 are asserted against a model client that
*fails the test if it is called at all* — not against a mock that counts calls
and hopes someone reads the count.

**The cap is in dollars, not in turns.** A turn counter does not protect against
one runaway turn, which is the failure that already happened once.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from os_system_agent.ask.router import (
    DEFAULT_COMMAND_PREFIX,
    RUNG_COMMAND,
    RUNG_KEYWORDS,
    AskRouter,
    InMemoryBudget,
    ModelReply,
    ModelRung,
    ModelsConfig,
    RouterError,
    load_models_config,
)

INTENTS = frozenset({"ventas_dia", "margen_categoria", "rotacion_sede"})
REGLAS: dict[str, list[str]] = {
    "ventas_dia": ["ventas", "cuanto vendimos"],
    "margen_categoria": ["margen"],
}

GRATIS = ModelRung(
    id="T2",
    provider="ollama_cloud",
    model="modelo-gratis",
    tier="free",
    max_cost_per_call_usd=Decimal(0),
    cost_per_1k_input_usd=Decimal(0),
    cost_per_1k_output_usd=Decimal(0),
)
PAGO = ModelRung(
    id="T3",
    provider="proveedor",
    model="modelo-de-pago",
    tier="paid",
    max_cost_per_call_usd=Decimal("0.05"),
    cost_per_1k_input_usd=Decimal("0.003"),
    cost_per_1k_output_usd=Decimal("0.015"),
)


def config(*rungs: ModelRung, cap: str = "1.00") -> ModelsConfig:
    return ModelsConfig(daily_cap_usd=Decimal(cap), max_question_chars=500, rungs=tuple(rungs))


class ModeloProhibido:
    """Un cliente cuyo uso ES el fallo del test."""

    def classify(self, question: str, rung: ModelRung) -> ModelReply:  # pragma: no cover
        raise AssertionError(f"no debia invocarse ningun modelo (rung={rung.id})")


class ModeloFalso:
    """Devuelve lo que se le indique y cuenta sus llamadas."""

    def __init__(self, intent: str = "ventas_dia", **kwargs: Any) -> None:
        self.reply = ModelReply(intent=intent, **kwargs)
        self.calls: list[str] = []

    def classify(self, question: str, rung: ModelRung) -> ModelReply:
        self.calls.append(rung.id)
        return self.reply


def router(
    *rungs: ModelRung,
    client: Any = None,
    budget: InMemoryBudget | None = None,
    cap: str = "1.00",
) -> AskRouter:
    return AskRouter(
        config=config(*rungs, cap=cap),
        allowed_intents=INTENTS,
        keyword_rules=REGLAS,
        budget=budget or InMemoryBudget(),
        model_client=client if client is not None else ModeloProhibido(),
    )


# --------------------------------------------------------------------------
# T0 — comando explicito, sin modelo.
# --------------------------------------------------------------------------


def test_t0_no_invoca_ningun_modelo() -> None:
    """El cliente de modelo lanza si lo llaman: esto lo prueba de verdad."""
    decision = router(GRATIS, PAGO).route(f"{DEFAULT_COMMAND_PREFIX} ventas_dia")
    assert decision.rung == RUNG_COMMAND
    assert decision.intent == "ventas_dia"
    assert decision.used_model is False
    assert decision.cost_usd == Decimal(0)


def test_t0_lee_los_parametros_del_comando() -> None:
    decision = router().route(f"{DEFAULT_COMMAND_PREFIX} ventas_dia fecha=2026-08-24 sede=norte")
    assert decision.params == {"fecha": "2026-08-24", "sede": "norte"}


def test_un_intent_equivocado_en_un_comando_no_escala_al_modelo() -> None:
    """El operador escribio un comando explicito; adivinar es lo que se evita."""
    modelo = ModeloFalso()
    decision = router(GRATIS, client=modelo).route(f"{DEFAULT_COMMAND_PREFIX} intent_inventado")
    assert decision.refused is True
    assert modelo.calls == []


def test_un_comando_sin_intent_explica_como_usarlo() -> None:
    decision = router().route(DEFAULT_COMMAND_PREFIX)
    assert decision.refused is True
    assert DEFAULT_COMMAND_PREFIX in (decision.message or "")


@pytest.mark.parametrize(
    "token", ["clave con espacio=x", "CLAVE=x", "k=" + "x" * 200, "=sinclave", "sinigual"]
)
def test_parametros_malformados_en_un_comando_son_rechazados(token: str) -> None:
    decision = router().route(f"{DEFAULT_COMMAND_PREFIX} ventas_dia {token}")
    assert decision.refused is True


# --------------------------------------------------------------------------
# T1 — palabras clave, tampoco toca un modelo.
# --------------------------------------------------------------------------


def test_t1_resuelve_por_palabras_clave_sin_modelo() -> None:
    decision = router(GRATIS, PAGO).route("¿cuanto vendimos ayer?")
    assert decision.rung == RUNG_KEYWORDS
    assert decision.intent == "ventas_dia"
    assert decision.used_model is False


def test_una_frase_ambigua_no_se_adivina() -> None:
    """Si dos intents encajan, adivinar da una cifra correcta de otra pregunta."""
    modelo = ModeloFalso()
    decision = router(GRATIS, client=modelo).route("dame ventas y margen")
    assert decision.intent != "ventas_dia" or decision.used_model is True


# --------------------------------------------------------------------------
# El modelo solo puede nombrar un id del enum cerrado.
# --------------------------------------------------------------------------


def test_un_intent_fuera_del_enum_es_rechazado() -> None:
    """El modelo no elige endpoints: nombra ids, y uno inventado no vale."""
    modelo = ModeloFalso(intent="borrar_todo")
    decision = router(GRATIS, client=modelo).route("una pregunta rara sin palabras clave")
    assert decision.refused is True
    assert decision.intent is None


def test_el_intent_rechazado_se_guarda_para_el_backlog() -> None:
    """Alimenta ask_intent_miss: la demanda real decide que intent se escribe."""
    modelo = ModeloFalso(intent="ventas_por_vendedor")
    decision = router(GRATIS, client=modelo).route("una pregunta rara sin palabras clave")
    assert decision.rejected_intent is not None


def test_un_intent_rechazado_no_arrastra_texto_peligroso_al_backlog() -> None:
    modelo = ModeloFalso(intent="x" * 500)
    decision = router(GRATIS, client=modelo).route("otra pregunta rara")
    assert decision.rejected_intent is None or len(decision.rejected_intent) < 200


def test_parametros_invalidos_del_modelo_son_rechazados() -> None:
    modelo = ModeloFalso(intent="ventas_dia", params={"MALA CLAVE": "x"})
    decision = router(GRATIS, client=modelo).route("pregunta libre sin palabras clave")
    assert decision.refused is True


def test_el_modelo_valido_si_enruta() -> None:
    modelo = ModeloFalso(intent="rotacion_sede", params={"sede": "norte"})
    decision = router(GRATIS, client=modelo).route("pregunta libre sin palabras clave")
    assert decision.intent == "rotacion_sede"
    assert decision.used_model is True
    assert modelo.calls == ["T2"]


# --------------------------------------------------------------------------
# RF-07 — el tope es en DOLARES, y la degradacion se dice.
# --------------------------------------------------------------------------


def test_rehusa_al_tope_en_vez_de_gastar() -> None:
    gastado = InMemoryBudget(spent=Decimal("1.00"))
    modelo = ModeloFalso()
    decision = router(PAGO, client=modelo, budget=gastado, cap="1.00").route(
        "pregunta libre sin palabras clave"
    )
    assert decision.refused is True
    assert modelo.calls == []


def test_la_degradacion_nunca_es_silenciosa() -> None:
    """Un tope que degrada sin explicarse produce un agente que 'a veces no sabe'."""
    gastado = InMemoryBudget(spent=Decimal("1.00"))
    decision = router(PAGO, client=ModeloFalso(), budget=gastado, cap="1.00").route(
        "pregunta libre sin palabras clave"
    )
    assert decision.message


def test_con_el_tope_gastado_el_escalon_gratuito_sigue_respondiendo() -> None:
    """El gratuito no gasta, asi que el tope no tiene por que apagarlo."""
    gastado = InMemoryBudget(spent=Decimal("1.00"))
    modelo = ModeloFalso(intent="rotacion_sede")
    decision = router(GRATIS, PAGO, client=modelo, budget=gastado, cap="1.00").route(
        "pregunta libre sin palabras clave"
    )
    assert decision.intent == "rotacion_sede"
    assert modelo.calls == ["T2"]


def test_el_escalon_de_pago_registra_lo_que_gasto() -> None:
    presupuesto = InMemoryBudget()
    modelo = ModeloFalso(intent="ventas_dia", input_tokens=1000, output_tokens=1000)
    router(PAGO, client=modelo, budget=presupuesto).route("pregunta libre sin palabras clave")
    assert presupuesto.spent > Decimal(0)


def test_el_escalon_gratuito_no_registra_gasto() -> None:
    presupuesto = InMemoryBudget()
    modelo = ModeloFalso(intent="ventas_dia", input_tokens=5000, output_tokens=5000)
    router(GRATIS, client=modelo, budget=presupuesto).route("pregunta libre sin palabras clave")
    assert presupuesto.spent == Decimal(0)


# --------------------------------------------------------------------------
# Sin modelo configurado el agente sigue siendo valido.
# --------------------------------------------------------------------------


def test_sin_cliente_de_modelo_los_escalones_deterministas_funcionan() -> None:
    sin_modelo = AskRouter(
        config=config(),
        allowed_intents=INTENTS,
        keyword_rules=REGLAS,
        budget=InMemoryBudget(),
        model_client=None,
    )
    assert sin_modelo.route(f"{DEFAULT_COMMAND_PREFIX} ventas_dia").intent == "ventas_dia"
    assert sin_modelo.route("cuanto vendimos").intent == "ventas_dia"


def test_sin_cliente_de_modelo_lo_ambiguo_se_rehusa_con_explicacion() -> None:
    sin_modelo = AskRouter(
        config=config(),
        allowed_intents=INTENTS,
        keyword_rules=REGLAS,
        budget=InMemoryBudget(),
        model_client=None,
    )
    decision = sin_modelo.route("una pregunta que ninguna regla cubre")
    assert decision.refused is True
    assert decision.message


# --------------------------------------------------------------------------
# route() no revienta: una excepcion en un turno se lee como "esta roto".
# --------------------------------------------------------------------------


@pytest.mark.parametrize("entrada", ["", "   ", None, 42, [], "x" * 10_000])
def test_una_pregunta_invalida_devuelve_rechazo_en_vez_de_excepcion(entrada: object) -> None:
    decision = router(GRATIS).route(entrada)  # type: ignore[arg-type]
    assert decision.refused is True
    assert decision.message


def test_un_modelo_que_falla_no_tumba_el_turno() -> None:
    class ModeloQueRevienta:
        def classify(self, question: str, rung: ModelRung) -> ModelReply:
            raise TimeoutError("el proveedor no respondio")

    decision = router(GRATIS, client=ModeloQueRevienta()).route("pregunta libre sin claves")
    assert decision.refused is True
    assert decision.message


# --------------------------------------------------------------------------
# Construccion: errores de configuracion se ven al arrancar, no en produccion.
# --------------------------------------------------------------------------


def test_un_enum_de_intents_vacio_es_rechazado() -> None:
    with pytest.raises(RouterError):
        AskRouter(
            config=config(),
            allowed_intents=frozenset(),
            keyword_rules={},
            budget=InMemoryBudget(),
        )


def test_una_regla_que_apunta_a_un_intent_inexistente_es_rechazada() -> None:
    """Jamas dispararia, y el sintoma seria 'no entiende', no 'hay una errata'."""
    with pytest.raises(RouterError):
        AskRouter(
            config=config(),
            allowed_intents=INTENTS,
            keyword_rules={"intent_que_no_existe": ["algo"]},
            budget=InMemoryBudget(),
        )


def test_un_prefijo_de_comando_invalido_es_rechazado() -> None:
    with pytest.raises(RouterError):
        AskRouter(
            config=config(),
            allowed_intents=INTENTS,
            keyword_rules={},
            budget=InMemoryBudget(),
            command_prefix="p",
        )


# --------------------------------------------------------------------------
# Carga de config de modelos.
# --------------------------------------------------------------------------


def write_models(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "ask-models.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


BASE_MODELS: dict[str, Any] = {
    "budget": {"daily_cap_usd": 1.0, "max_question_chars": 500},
    "rungs": [
        {
            "id": "T2",
            "provider": "ollama_cloud",
            "model": "m",
            "tier": "free",
            "max_cost_per_call_usd": 0,
            "cost_per_1k_input_usd": 0,
            "cost_per_1k_output_usd": 0,
            "available": True,
        }
    ],
}


def test_el_ejemplo_del_repo_carga() -> None:
    ejemplo = Path(__file__).resolve().parents[1] / "config" / "ask-models.example.yml"
    assert load_models_config(ejemplo).daily_cap_usd > 0


def test_ollama_local_se_omite_porque_no_esta_instalado() -> None:
    """Verificado con `ollama list`: ningun box lo tiene.

    Configurar un escalon que no existe produce un fallo en tiempo de pregunta,
    que es el peor momento para descubrirlo.
    """
    ejemplo = Path(__file__).resolve().parents[1] / "config" / "ask-models.example.yml"
    proveedores = {rung.provider for rung in load_models_config(ejemplo).rungs}
    assert "ollama_local" not in proveedores


def test_un_escalon_marcado_no_disponible_se_omite(tmp_path: Path) -> None:
    data = {**BASE_MODELS, "rungs": [{**BASE_MODELS["rungs"][0], "available": False}]}
    assert load_models_config(write_models(tmp_path, data)).rungs == ()


def test_un_escalon_gratuito_con_tarifa_no_nula_es_rechazado(tmp_path: Path) -> None:
    """'Gratis' con tarifa es una contradiccion que se paga en la factura."""
    data = {**BASE_MODELS, "rungs": [{**BASE_MODELS["rungs"][0], "cost_per_1k_input_usd": 0.01}]}
    with pytest.raises(RouterError):
        load_models_config(write_models(tmp_path, data))


@pytest.mark.parametrize("cap", [-1, "gratis", None])
def test_un_tope_invalido_es_rechazado(tmp_path: Path, cap: object) -> None:
    data = {**BASE_MODELS, "budget": {"daily_cap_usd": cap, "max_question_chars": 500}}
    with pytest.raises(RouterError):
        load_models_config(write_models(tmp_path, data))


@pytest.mark.parametrize("rung_id", ["T0", "T1"])
def test_no_se_puede_reusar_el_id_de_un_escalon_determinista(tmp_path: Path, rung_id: str) -> None:
    """T0 y T1 no tienen modelo; reusar su id haria ilegible cualquier log."""
    data = {**BASE_MODELS, "rungs": [{**BASE_MODELS["rungs"][0], "id": rung_id}]}
    with pytest.raises(RouterError):
        load_models_config(write_models(tmp_path, data))


def test_un_archivo_inexistente_falla_cerrado(tmp_path: Path) -> None:
    with pytest.raises(RouterError):
        load_models_config(tmp_path / "no-existe.yml")


def test_el_presupuesto_en_memoria_no_es_para_produccion() -> None:
    """Un presupuesto que se olvida al reiniciar le da barra libre a un crash-loop."""
    assert "tests" in (InMemoryBudget.__doc__ or "").lower()
