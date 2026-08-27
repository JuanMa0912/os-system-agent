"""Tests for timezone-explicit business dates (spec 006 RNF-01, plan §5).

The bug this module exists to prevent is the quietest one in the whole spec: the
repo runs in UTC, the business runs in local time, and a "yesterday" computed
from the host clock returns the wrong day for several hours of every day — with
plausible figures and nobody noticing.

So the centre of gravity here is the day-border tests. They use a **fixed
offset** rather than an IANA name on purpose: this host has no tz database, and
a test that skips is a test that never caught anything.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from os_system_agent.ask.dateparse import (
    SUPPORTED_EXPRESSIONS,
    DateExpressionError,
    DateRange,
    load_zone,
    now_local,
    resolve_range,
    today_local,
)

# Colombia is UTC-5 year round (no DST), so a fixed offset is faithful here and
# does not depend on a tz database being installed.
BOGOTA = timezone(timedelta(hours=-5), "UTC-5")

try:  # pragma: no cover - depends on the host
    ZoneInfo("America/Bogota")
    HAY_TZDATA = True
except Exception:  # noqa: BLE001 - cualquier fallo significa "no disponible"
    HAY_TZDATA = False

necesita_tzdata = pytest.mark.skipif(
    not HAY_TZDATA, reason="este host no tiene base de datos de zonas horarias"
)


# --------------------------------------------------------------------------
# El borde del dia — la razon de ser del modulo.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utc, dia_negocio, dia_utc",
    [
        # 22:30 local del 24 = 03:30 UTC del 25. El calculo ingenuo diria "25".
        (datetime(2026, 8, 25, 3, 30, tzinfo=UTC), date(2026, 8, 24), date(2026, 8, 25)),
        # 23:30 local del 24 = 04:30 UTC del 25. Sigue siendo el 24 para el negocio.
        (datetime(2026, 8, 25, 4, 30, tzinfo=UTC), date(2026, 8, 24), date(2026, 8, 25)),
        # 00:30 local del 25 = 05:30 UTC del 25. Aqui si cambia el dia de negocio.
        (datetime(2026, 8, 25, 5, 30, tzinfo=UTC), date(2026, 8, 25), date(2026, 8, 25)),
        # 19:00 local del 24 = 00:00 UTC del 25: el UTC ya cambio de dia, el negocio no.
        (datetime(2026, 8, 25, 0, 0, tzinfo=UTC), date(2026, 8, 24), date(2026, 8, 25)),
    ],
)
def test_el_dia_de_negocio_no_es_el_dia_utc(
    utc: datetime, dia_negocio: date, dia_utc: date
) -> None:
    assert today_local(BOGOTA, now=utc) == dia_negocio
    # Y se comprueba que el calculo ingenuo habria dado otra cosa: si estos dos
    # coincidieran, el caso no estaria probando nada.
    assert utc.date() == dia_utc


def test_ayer_en_el_borde_no_se_corre_un_dia() -> None:
    """A las 23:30 locales, "ayer" es el dia anterior local, no el anterior UTC."""
    a_las_2330_locales = datetime(2026, 8, 25, 4, 30, tzinfo=UTC)
    rango = resolve_range("ayer", BOGOTA, now=a_las_2330_locales)
    assert rango.iso() == ("2026-08-23", "2026-08-23")


def test_now_local_convierte_a_la_zona_pedida() -> None:
    momento = now_local(BOGOTA, now=datetime(2026, 8, 25, 5, 30, tzinfo=UTC))
    assert momento.hour == 0
    assert momento.date() == date(2026, 8, 25)


# --------------------------------------------------------------------------
# Un datetime sin zona es exactamente como vuelve el bug.
# --------------------------------------------------------------------------


def test_un_now_sin_zona_es_rechazado() -> None:
    with pytest.raises(DateExpressionError, match="timezone-aware"):
        today_local(BOGOTA, now=datetime(2026, 8, 25, 12, 0))


def test_resolve_range_tambien_rechaza_un_now_sin_zona() -> None:
    with pytest.raises(DateExpressionError):
        resolve_range("ayer", BOGOTA, now=datetime(2026, 8, 25, 12, 0))


# --------------------------------------------------------------------------
# La zona falla cerrado: nunca cae a UTC ni a la del host.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "Continente/Ciudad_Que_No_Existe"])
def test_una_zona_invalida_falla_en_vez_de_caer_a_utc(bad: str) -> None:
    """Caer a UTC "para seguir funcionando" desplazaria todos los dias en silencio."""
    with pytest.raises(DateExpressionError):
        load_zone(bad)


def test_el_error_de_zona_desconocida_menciona_tzdata() -> None:
    """En un host sin base de zonas el sintoma es identico a un typo.

    Sin esa pista, diagnosticarlo cuesta una tarde.
    """
    with pytest.raises(DateExpressionError, match="tzdata"):
        load_zone("America/Bogota_Inexistente")


@necesita_tzdata
def test_una_zona_iana_valida_se_resuelve() -> None:
    assert today_local("America/Bogota", now=datetime(2026, 8, 25, 4, 30, tzinfo=UTC)) == date(
        2026, 8, 24
    )


# --------------------------------------------------------------------------
# Expresiones relativas.
# --------------------------------------------------------------------------

# Martes 25 de agosto de 2026, 12:00 locales.
MARTES = datetime(2026, 8, 25, 17, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "expr, esperado",
    [
        ("hoy", ("2026-08-25", "2026-08-25")),
        ("ayer", ("2026-08-24", "2026-08-24")),
        # La semana ISO empieza en lunes; "esta semana" se corta HOY porque no
        # hay dato de manana.
        ("esta semana", ("2026-08-24", "2026-08-25")),
        ("semana pasada", ("2026-08-17", "2026-08-23")),
        ("este mes", ("2026-08-01", "2026-08-25")),
        ("mes pasado", ("2026-07-01", "2026-07-31")),
    ],
)
def test_expresiones_relativas(expr: str, esperado: tuple[str, str]) -> None:
    assert resolve_range(expr, BOGOTA, now=MARTES).iso() == esperado


def test_esta_semana_no_llega_a_manana() -> None:
    """Pedir dias futuros solo produce una respuesta vacia y confusa."""
    rango = resolve_range("esta semana", BOGOTA, now=MARTES)
    assert rango.end == today_local(BOGOTA, now=MARTES)


def test_mes_pasado_cruzando_el_ano() -> None:
    primero_de_enero = datetime(2026, 1, 1, 17, 0, tzinfo=UTC)
    assert resolve_range("mes pasado", BOGOTA, now=primero_de_enero).iso() == (
        "2025-12-01",
        "2025-12-31",
    )


def test_semana_pasada_desde_un_lunes() -> None:
    """Un lunes es el borde: "semana pasada" no debe incluir hoy."""
    lunes = datetime(2026, 8, 24, 17, 0, tzinfo=UTC)
    assert resolve_range("semana pasada", BOGOTA, now=lunes).iso() == ("2026-08-17", "2026-08-23")


def test_este_mes_desde_el_dia_uno() -> None:
    primero = datetime(2026, 8, 1, 17, 0, tzinfo=UTC)
    assert resolve_range("este mes", BOGOTA, now=primero).iso() == ("2026-08-01", "2026-08-01")


# --------------------------------------------------------------------------
# Normalizacion de la frase.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "escrito",
    ["ayer", "AYER", "  Ayer  ", "¿ayer?", "el dia de ayer", "El Día De Ayer", "dia de ayer."],
)
def test_variantes_de_escritura_resuelven_igual(escrito: str) -> None:
    """Acentos, mayusculas y signos no deben cambiar el resultado."""
    assert resolve_range(escrito, BOGOTA, now=MARTES).iso() == ("2026-08-24", "2026-08-24")


@pytest.mark.parametrize(
    "alias, canonico",
    [
        ("semana actual", "esta semana"),
        ("semana anterior", "semana pasada"),
        ("mes actual", "este mes"),
        ("mes anterior", "mes pasado"),
    ],
)
def test_los_alias_llegan_a_la_misma_expresion(alias: str, canonico: str) -> None:
    assert resolve_range(alias, BOGOTA, now=MARTES).expression == canonico


# --------------------------------------------------------------------------
# Fechas explicitas.
# --------------------------------------------------------------------------


def test_dia_iso_explicito() -> None:
    rango = resolve_range("2026-08-20", BOGOTA, now=MARTES)
    assert rango.iso() == ("2026-08-20", "2026-08-20")
    assert rango.partial is False


def test_rango_iso_explicito() -> None:
    assert resolve_range("2026-08-01..2026-08-15", BOGOTA, now=MARTES).iso() == (
        "2026-08-01",
        "2026-08-15",
    )


def test_rango_iso_con_espacios() -> None:
    assert resolve_range("2026-08-01 .. 2026-08-15", BOGOTA, now=MARTES).iso() == (
        "2026-08-01",
        "2026-08-15",
    )


def test_un_rango_al_reves_es_rechazado() -> None:
    with pytest.raises(DateExpressionError, match="starts after"):
        resolve_range("2026-08-15..2026-08-01", BOGOTA, now=MARTES)


@pytest.mark.parametrize("bad", ["2026-02-30", "2026-13-01", "2026-00-10"])
def test_una_fecha_imposible_es_rechazada(bad: str) -> None:
    with pytest.raises(DateExpressionError):
        resolve_range(bad, BOGOTA, now=MARTES)


@pytest.mark.parametrize("bad", ["25-08-2026", "2026/08/25", "20260825", "2026-8-5"])
def test_otros_formatos_de_fecha_no_se_adivinan(bad: str) -> None:
    """Adivinar dia/mes seria elegir en silencio entre dos lecturas validas."""
    with pytest.raises(DateExpressionError):
        resolve_range(bad, BOGOTA, now=MARTES)


# --------------------------------------------------------------------------
# Fallo cerrado: lo no reconocido NUNCA se vuelve "hoy".
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr", ["", "   ", "la semana antepasada", "ultimos 3 dias", "en junio", "cualquier cosa"]
)
def test_una_expresion_no_soportada_falla_en_vez_de_asumir_hoy(expr: str) -> None:
    with pytest.raises(DateExpressionError):
        resolve_range(expr, BOGOTA, now=MARTES)


def test_el_error_enumera_lo_que_si_se_entiende() -> None:
    """El mensaje tiene que ser accionable para quien pregunta por chat."""
    with pytest.raises(DateExpressionError) as exc:
        resolve_range("la semana antepasada", BOGOTA, now=MARTES)
    for soportada in ("hoy", "ayer", "mes pasado"):
        assert soportada in str(exc.value)
    assert set(SUPPORTED_EXPRESSIONS)


# --------------------------------------------------------------------------
# El mensaje de error no puede filtrar lo que alguien pego en el chat.
# --------------------------------------------------------------------------


def test_el_error_no_devuelve_un_secreto_pegado_en_el_chat() -> None:
    """Redactar ANTES de truncar: truncar primero parte el token por la mitad,
    y medio token ya no casa con los patrones — se filtraria."""
    pegado = "dame ventas de password=SuperSecreta123456 por favor"
    with pytest.raises(DateExpressionError) as exc:
        resolve_range(pegado, BOGOTA, now=MARTES)
    assert "SuperSecreta123456" not in str(exc.value)


def test_el_error_no_crece_sin_limite_con_una_frase_larga() -> None:
    with pytest.raises(DateExpressionError) as exc:
        resolve_range("x" * 5000, BOGOTA, now=MARTES)
    assert len(str(exc.value)) < 500


# --------------------------------------------------------------------------
# `partial`: RF-05 prohibe presentar un agregado parcial como completo.
# --------------------------------------------------------------------------


def test_un_rango_que_llega_a_hoy_se_marca_parcial() -> None:
    assert resolve_range("hoy", BOGOTA, now=MARTES).partial is True
    assert resolve_range("esta semana", BOGOTA, now=MARTES).partial is True
    assert resolve_range("este mes", BOGOTA, now=MARTES).partial is True


def test_un_rango_ya_cerrado_no_es_parcial() -> None:
    assert resolve_range("ayer", BOGOTA, now=MARTES).partial is False
    assert resolve_range("semana pasada", BOGOTA, now=MARTES).partial is False
    assert resolve_range("mes pasado", BOGOTA, now=MARTES).partial is False


def test_daterange_es_inmutable() -> None:
    rango = resolve_range("ayer", BOGOTA, now=MARTES)
    with pytest.raises(AttributeError):
        rango.start = date(2020, 1, 1)  # type: ignore[misc]
    assert isinstance(rango, DateRange)
