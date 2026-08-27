"""Tests for the intent catalog (spec 006 RF-02/RF-03, plan §3.1 and §3.3).

This catalog *is* the security control, not a convenience: the model may only
name an ``id`` that already exists here, and everything else — exact route,
method, typed parameters, dropped fields, template and cut-off — comes from the
YAML, which is reviewed in the PR diff.

So every test below is really the same assertion in a different disguise: a
catalog that does not hold the line **must not load at all**. A degraded catalog
would be worse than none, because the agent would run with a control that looks
present and is not.
"""

from __future__ import annotations

import copy
from datetime import UTC, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from os_system_agent.ask.dateparse import DateExpressionError
from os_system_agent.ask.intents import (
    SUPPORTED_VERSION,
    AskCatalogError,
    ParamType,
    load_ask_catalog,
)

# Este host no trae base de datos de zonas horarias, y el cargador acepta un
# resolvedor inyectable justamente para eso.
FAKE_TZ = timezone(timedelta(hours=-5), "UTC-5")


def fake_zone(name: str) -> Any:
    if name == "America/Bogota":
        return FAKE_TZ
    raise DateExpressionError(f"unknown timezone {name!r}")


BASE: dict[str, Any] = {
    "version": SUPPORTED_VERSION,
    "zona_horaria": "America/Bogota",
    "intents": [
        {
            "id": "ventas_cobertura",
            "descripcion": "Hasta que fecha hay ventas cargadas.",
            "ruta": "/api/ventas-x-item/v2",
            "metodo": "GET",
            "parametros": [
                {
                    "nombre": "mode",
                    "tipo": "enum",
                    "obligatorio": True,
                    "valores": ["meta"],
                    "fijo": "meta",
                }
            ],
            "drop_fields": ["source"],
            "fecha_corte": {"campo": "maxDate"},
            "plantilla": "Hay datos hasta el {maxDate}.",
        }
    ],
}


def write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "ask-queries.yml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def load(tmp_path: Path, mutate: Any = None) -> Any:
    data = copy.deepcopy(BASE)
    if mutate is not None:
        mutate(data)
    return load_ask_catalog(write(tmp_path, data), resolve_zone=fake_zone)


def load_expecting_failure(tmp_path: Path, mutate: Any) -> None:
    with pytest.raises(AskCatalogError):
        load(tmp_path, mutate)


# --------------------------------------------------------------------------
# El caso feliz, para que los negativos signifiquen algo.
# --------------------------------------------------------------------------


def test_un_catalogo_valido_carga(tmp_path: Path) -> None:
    catalogo = load(tmp_path)
    assert catalogo.version == SUPPORTED_VERSION
    assert len(catalogo.intents) == 1
    assert catalogo.get("ventas_cobertura").route == "/api/ventas-x-item/v2"


def test_routes_es_el_allowlist_de_rutas(tmp_path: Path) -> None:
    """El cliente HTTP consume esta propiedad: es la lista de rutas alcanzables."""
    assert load(tmp_path).routes == ("/api/ventas-x-item/v2",)


def test_un_id_desconocido_falla_cerrado(tmp_path: Path) -> None:
    """El modelo solo puede nombrar ids; uno inventado no debe resolver a nada."""
    with pytest.raises(AskCatalogError):
        load(tmp_path).get("intent_que_no_existe")


def test_el_id_desconocido_no_se_devuelve_crudo_en_el_error(tmp_path: Path) -> None:
    """El id viene de un chat: podria traer algo que no debe quedar en un log."""
    catalogo = load(tmp_path)
    with pytest.raises(AskCatalogError) as exc:
        catalogo.get("password=SuperSecreta123456")
    assert "SuperSecreta123456" not in str(exc.value)


# --------------------------------------------------------------------------
# drop_fields OBLIGATORIO — el control que impide que un campo salga del proceso.
# --------------------------------------------------------------------------


def test_drop_fields_obligatorio(tmp_path: Path) -> None:
    """Omitir la clave es descuido y no debe pasar por decision deliberada."""
    load_expecting_failure(tmp_path, lambda d: d["intents"][0].pop("drop_fields"))


def test_una_lista_vacia_explicita_si_es_valida(tmp_path: Path) -> None:
    """`[]` es una decision que se ve en el diff del PR; omitirla no."""
    catalogo = load(tmp_path, lambda d: d["intents"][0].update(drop_fields=[]))
    assert catalogo.intents[0].drop_fields == ()


@pytest.mark.parametrize("malo", ["source", {"a": 1}, [1, 2], ["campo con espacio"], [""]])
def test_drop_fields_malformado_es_rechazado(tmp_path: Path, malo: object) -> None:
    load_expecting_failure(tmp_path, lambda d: d["intents"][0].update(drop_fields=malo))


def test_drop_fields_solo_admite_nombres_planos(tmp_path: Path) -> None:
    """Un nombre plano borra el campo este donde este anidado.

    Es mas seguro que una ruta con punto: una ruta solo tapa la aparicion que
    alguien se acordo de escribir, y basta con que el portal mueva el campo un
    nivel para que el dato personal vuelva a salir.
    """
    load_expecting_failure(tmp_path, lambda d: d["intents"][0].update(drop_fields=["meta.source"]))


def test_drop_fields_no_admite_repetidos(tmp_path: Path) -> None:
    load_expecting_failure(
        tmp_path, lambda d: d["intents"][0].update(drop_fields=["source", "source"])
    )


# --------------------------------------------------------------------------
# Ruta EXACTA — conceder por prefijo concederia endpoints que exponen cedulas.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ruta",
    [
        "/api/margenes/*",  # comodin
        "/api/margenes/",  # barra final: dos formas de la misma ruta
        "/api/{id}/data",  # plantilla de path
        "/api/margenes?mode=meta",  # query pegada
        "https://portal.example.com/api/x",  # URL absoluta
        "//portal.example.com/api/x",  # relativa al protocolo
        "/api/../admin/users",
        "api/margenes",  # sin barra inicial
        "/margenes",  # fuera de /api
        "",
        "/api/margenes data",  # espacio
    ],
)
def test_una_ruta_que_no_es_exacta_es_rechazada(tmp_path: Path, ruta: str) -> None:
    load_expecting_failure(tmp_path, lambda d: d["intents"][0].update(ruta=ruta))


# --------------------------------------------------------------------------
# Solo GET — el portal no sabe decir "solo lectura".
# --------------------------------------------------------------------------


@pytest.mark.parametrize("metodo", ["POST", "PUT", "PATCH", "DELETE", "HEAD", ""])
def test_ningun_metodo_distinto_de_get_carga(tmp_path: Path, metodo: str) -> None:
    load_expecting_failure(tmp_path, lambda d: d["intents"][0].update(metodo=metodo))


def test_el_metodo_get_queda_normalizado(tmp_path: Path) -> None:
    assert load(tmp_path, lambda d: d["intents"][0].update(metodo="get")).intents[0].method == "GET"


# --------------------------------------------------------------------------
# fecha_corte OBLIGATORIA — una cifra sin su fecha engana (RF-04).
# --------------------------------------------------------------------------


def test_fecha_corte_obligatoria(tmp_path: Path) -> None:
    load_expecting_failure(tmp_path, lambda d: d["intents"][0].pop("fecha_corte"))


def test_una_fecha_corte_prestada_debe_apuntar_a_un_intent_existente(tmp_path: Path) -> None:
    """Si la respuesta no trae su fecha, otro intent la aporta — pero tiene que existir."""
    load_expecting_failure(
        tmp_path,
        lambda d: d["intents"][0].update(
            fecha_corte={"campo": "maxDate", "intent": "intent_inexistente"}
        ),
    )


def test_una_fecha_corte_prestada_valida_carga(tmp_path: Path) -> None:
    def mutate(d: dict[str, Any]) -> None:
        segundo = copy.deepcopy(d["intents"][0])
        segundo["id"] = "ventas_resumen"
        segundo["fecha_corte"] = {"campo": "maxDate", "intent": "ventas_cobertura"}
        d["intents"].append(segundo)

    catalogo = load(tmp_path, mutate)
    assert catalogo.get("ventas_resumen").cutoff.from_intent == "ventas_cobertura"


# --------------------------------------------------------------------------
# Parametros: `fijo` clava el valor para que el modelo no lo cambie.
# --------------------------------------------------------------------------


def test_un_parametro_fijo_no_lo_aporta_el_modelo(tmp_path: Path) -> None:
    """Es lo que impide que `mode=meta` se vuelva `mode=vendedor` en la misma ruta."""
    param = load(tmp_path).intents[0].params[0]
    assert param.fixed == "meta"
    assert param.model_supplied is False


def test_un_parametro_sin_fijo_si_lo_aporta_el_modelo(tmp_path: Path) -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["intents"][0]["parametros"].append(
            {"nombre": "start", "tipo": "fecha", "obligatorio": True}
        )

    param = load(tmp_path, mutate).intents[0].params[1]
    assert param.model_supplied is True
    assert param.type is ParamType.DATE


def test_un_valor_fijo_fuera_de_los_permitidos_es_rechazado(tmp_path: Path) -> None:
    """Un `fijo` que contradice su propio enum es una contradiccion silenciosa."""
    load_expecting_failure(
        tmp_path,
        lambda d: d["intents"][0]["parametros"][0].update(fijo="vendedor"),
    )


@pytest.mark.parametrize(
    "param",
    [
        {"nombre": "", "tipo": "texto", "obligatorio": True},
        {"nombre": "con-guion", "tipo": "texto", "obligatorio": True},
        {"nombre": "1numero", "tipo": "texto", "obligatorio": True},
        {"nombre": "ok", "tipo": "tipo_inventado", "obligatorio": True},
        {"nombre": "ok", "tipo": "texto"},  # falta obligatorio
        {"nombre": "ok", "tipo": "enum", "obligatorio": True},  # enum sin valores
    ],
)
def test_un_parametro_malformado_es_rechazado(tmp_path: Path, param: dict[str, Any]) -> None:
    load_expecting_failure(tmp_path, lambda d: d["intents"][0].update(parametros=[param]))


def test_dos_parametros_con_el_mismo_nombre_son_rechazados(tmp_path: Path) -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["intents"][0]["parametros"].append(
            {"nombre": "mode", "tipo": "texto", "obligatorio": False}
        )

    load_expecting_failure(tmp_path, mutate)


# --------------------------------------------------------------------------
# Plantilla.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("plantilla", ["", "   ", "Sin ningun placeholder"])
def test_una_plantilla_inutil_es_rechazada(tmp_path: Path, plantilla: str) -> None:
    """Una plantilla sin campos no responde nada: es un intent muerto."""
    load_expecting_failure(tmp_path, lambda d: d["intents"][0].update(plantilla=plantilla))


def test_una_plantilla_con_llaves_desbalanceadas_es_rechazada(tmp_path: Path) -> None:
    load_expecting_failure(
        tmp_path, lambda d: d["intents"][0].update(plantilla="Hasta el {maxDate")
    )


# --------------------------------------------------------------------------
# Integridad global del catalogo.
# --------------------------------------------------------------------------


def test_ids_duplicados_son_rechazados(tmp_path: Path) -> None:
    """Con ids repetidos, cual gana seria un detalle de implementacion."""
    load_expecting_failure(tmp_path, lambda d: d["intents"].append(copy.deepcopy(d["intents"][0])))


@pytest.mark.parametrize("intents", [[], None, "no es lista", {}])
def test_una_lista_de_intents_vacia_o_invalida_es_rechazada(
    tmp_path: Path, intents: object
) -> None:
    load_expecting_failure(tmp_path, lambda d: d.update(intents=intents))


@pytest.mark.parametrize("version", [None, 0, 999, "1", True])
def test_una_version_no_soportada_es_rechazada(tmp_path: Path, version: object) -> None:
    load_expecting_failure(tmp_path, lambda d: d.update(version=version))


@pytest.mark.parametrize("zona", [None, "", "   ", "Zona/Inventada"])
def test_una_zona_horaria_ausente_o_desconocida_es_rechazada(tmp_path: Path, zona: object) -> None:
    """Sin zona no hay dia de negocio, y caer al reloj del host es el bug de RNF-01."""
    load_expecting_failure(tmp_path, lambda d: d.update(zona_horaria=zona))


def test_un_archivo_inexistente_falla_cerrado(tmp_path: Path) -> None:
    with pytest.raises(AskCatalogError):
        load_ask_catalog(tmp_path / "no-existe.yml", resolve_zone=fake_zone)


def test_un_yaml_invalido_falla_cerrado(tmp_path: Path) -> None:
    path = tmp_path / "roto.yml"
    path.write_text("version: 1\n  intents: [", encoding="utf-8")
    with pytest.raises(AskCatalogError):
        load_ask_catalog(path, resolve_zone=fake_zone)


def test_un_yaml_que_no_es_mapping_falla_cerrado(tmp_path: Path) -> None:
    path = tmp_path / "lista.yml"
    path.write_text("- uno\n- dos\n", encoding="utf-8")
    with pytest.raises(AskCatalogError):
        load_ask_catalog(path, resolve_zone=fake_zone)


# --------------------------------------------------------------------------
# El ejemplo versionado tiene que ser valido: es lo que se revisa en el PR.
# --------------------------------------------------------------------------


def test_el_ejemplo_del_repo_carga() -> None:
    ejemplo = Path(__file__).resolve().parents[1] / "config" / "ask-queries.example.yml"
    catalogo = load_ask_catalog(ejemplo, resolve_zone=fake_zone)
    assert catalogo.intents


def test_el_ejemplo_no_concede_el_path_de_margenes_con_datos_personales() -> None:
    """/api/margenes/data son 13 endpoints; varios devuelven NIT y cedula.

    Este test es la barandilla de esa decision: si alguien lo anade sin pensar,
    el test lo dice antes que una auditoria.
    """
    ejemplo = Path(__file__).resolve().parents[1] / "config" / "ask-queries.example.yml"
    catalogo = load_ask_catalog(ejemplo, resolve_zone=fake_zone)
    assert "/api/margenes/data" not in catalogo.routes


def test_el_ejemplo_solo_declara_rutas_get() -> None:
    ejemplo = Path(__file__).resolve().parents[1] / "config" / "ask-queries.example.yml"
    catalogo = load_ask_catalog(ejemplo, resolve_zone=fake_zone)
    assert {intent.method for intent in catalogo.intents} == {"GET"}


def test_el_ejemplo_declara_drop_fields_y_fecha_corte_en_todos() -> None:
    ejemplo = Path(__file__).resolve().parents[1] / "config" / "ask-queries.example.yml"
    catalogo = load_ask_catalog(ejemplo, resolve_zone=fake_zone)
    for intent in catalogo.intents:
        assert intent.cutoff.field, intent.id
        assert isinstance(intent.drop_fields, tuple), intent.id


def test_utc_no_es_la_zona_del_ejemplo() -> None:
    """Si el ejemplo dijera UTC, copiarlo reintroduciria el bug del dia corrido."""
    ejemplo = Path(__file__).resolve().parents[1] / "config" / "ask-queries.example.yml"
    catalogo = load_ask_catalog(ejemplo, resolve_zone=fake_zone)
    assert catalogo.timezone != "UTC"
    assert UTC is not None  # el import se usa para documentar el contraste
