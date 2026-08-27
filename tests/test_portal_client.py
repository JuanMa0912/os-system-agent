"""Tests for the read-only portal client (spec 006 RF-03, CA-01, CA-06).

The portal's permission model cannot express "read only": the same subdashboard
permission enables ``GET`` and ``POST``/``PATCH`` alike. The method allowlist in
this client is therefore not defence in depth — it is *the* control that stops
the agent's session from writing.

So the central assertion here is not "a POST raises". It is that **the fetcher is
never reached**: a rejected request must die before a URL is even built, because
anything that reaches the transport has already left the process.
"""

from __future__ import annotations

import pytest

from os_system_agent.portal.client import (
    ALLOWED_METHODS,
    HEALTH_PATH,
    MAX_RETRIES,
    EndpointCall,
    HttpResponse,
    PortalClient,
    assert_method_allowed,
    assert_params_allowed,
    assert_path_allowed,
    check_health,
    normalize_base_url,
)
from os_system_agent.portal.errors import (
    InvalidParameterError,
    MethodNotAllowedError,
    PathNotAllowedError,
    PortalConfigError,
    TerminalAuthError,
)

BASE = "https://portal.example.com"
COOKIE = "vp_session=super-secreto-que-no-debe-aparecer"


class SpyFetcher:
    """Records every call it receives, so "never reached" is assertable."""

    def __init__(self, response: HttpResponse | None = None) -> None:
        self.calls: list[EndpointCall] = []
        self._response = response or HttpResponse(status=200, body="{}")

    def __call__(self, call: EndpointCall) -> HttpResponse:
        self.calls.append(call)
        return self._response


def make_client(
    fetcher: SpyFetcher | None = None,
    *,
    allowed: frozenset[str] | None = None,
) -> tuple[PortalClient, SpyFetcher]:
    spy = fetcher or SpyFetcher()
    client = PortalClient(
        base_url=BASE,
        fetcher=spy,
        allowed_paths=allowed if allowed is not None else frozenset({HEALTH_PATH, "/api/costos"}),
    )
    return client, spy


# --------------------------------------------------------------------------
# CA-01 — el allowlist de metodo, que es LA frontera de escritura.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"])
def test_ningun_metodo_de_escritura_llega_al_transporte(method: str) -> None:
    """Lo que importa no es que lance, sino que el fetcher NUNCA se invoque."""
    client, spy = make_client()
    with pytest.raises(MethodNotAllowedError):
        client.request(method, HEALTH_PATH)
    assert spy.calls == [], f"{method} alcanzo el transporte: ya salio del proceso"


def test_solo_get_esta_permitido() -> None:
    assert ALLOWED_METHODS == frozenset({"GET"})


def test_el_metodo_se_valida_antes_que_la_ruta() -> None:
    """Un POST a una ruta prohibida debe fallar por el METODO.

    El orden es parte del contrato: si la ruta se validara primero, un POST a
    una ruta permitida pasaria mas lejos de lo debido antes de morir.
    """
    client, spy = make_client()
    with pytest.raises(MethodNotAllowedError):
        client.request("POST", "/api/ruta-prohibida")
    assert spy.calls == []


@pytest.mark.parametrize("raw", ["get", "  GET  ", "Get"])
def test_get_se_normaliza(raw: str) -> None:
    assert assert_method_allowed(raw) == "GET"


@pytest.mark.parametrize("bad", ["", "   ", None, 42, "GET POST"])
def test_metodo_malformado_es_rechazado(bad: object) -> None:
    with pytest.raises(MethodNotAllowedError):
        assert_method_allowed(bad)  # type: ignore[arg-type]


def test_no_hay_reintentos() -> None:
    """Un reintento del cliente multiplicaria el consumo del limite compartido."""
    assert MAX_RETRIES == 0


# --------------------------------------------------------------------------
# Allowlist de ruta: coincidencia EXACTA, nunca por prefijo.
# --------------------------------------------------------------------------


def test_la_ruta_es_exacta_no_por_prefijo() -> None:
    """`/api/margenes/data` son 13 endpoints bajo un mismo path.

    Conceder por prefijo concederia todos, incluidos los que exponen NIT de
    cliente y cedula de vendedor.
    """
    permitidas = frozenset({"/api/margenes"})
    assert assert_path_allowed("/api/margenes", permitidas) == "/api/margenes"
    with pytest.raises(PathNotAllowedError):
        assert_path_allowed("/api/margenes/data", permitidas)


@pytest.mark.parametrize(
    "path",
    [
        "//evil.example/api",  # protocol-relative: cambiaria de host
        "/api/../admin/users",  # sale del prefijo permitido
        "https://evil.example",  # URL absoluta
        "/api\\costos",  # separador de Windows
        "/api/costos?admin=1",  # query pegada a la ruta
        "/api/costos#frag",
        "/api/ costos",
        "/api/costos\n/api/admin",  # inyeccion de linea
        "api/costos",  # sin barra inicial
        "",
    ],
)
def test_rutas_con_forma_hostil_son_rechazadas(path: str) -> None:
    client, spy = make_client(allowed=frozenset({"/api/costos"}))
    with pytest.raises(PathNotAllowedError):
        client.get(path)
    assert spy.calls == []


def test_ruta_permitida_si_llega_al_transporte() -> None:
    client, spy = make_client()
    client.get(HEALTH_PATH)
    assert len(spy.calls) == 1
    assert spy.calls[0].path == HEALTH_PATH


# --------------------------------------------------------------------------
# Parametros: vienen del catalogo, nunca de un modelo ni de un chat (RF-02).
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"": "x"},
        {"1nombre": "x"},
        {"con-guion": "x"},
        {"nombre": "x" * 500},
        {"nombre": "valor\x00nulo"},
        {"nombre": 42},
        {"nombre": None},
    ],
)
def test_parametros_invalidos_son_rechazados(params: dict[str, object]) -> None:
    client, spy = make_client()
    with pytest.raises(InvalidParameterError):
        client.get(HEALTH_PATH, params=params)  # type: ignore[arg-type]
    assert spy.calls == []


def test_los_parametros_quedan_ordenados_de_forma_estable() -> None:
    """Estable = la misma consulta produce la misma URL, y por tanto la misma
    entrada de ledger y la misma clave de cache."""
    primero = assert_params_allowed({"b": "2", "a": "1"}, path="/x")
    segundo = assert_params_allowed({"a": "1", "b": "2"}, path="/x")
    assert primero == segundo == (("a", "1"), ("b", "2"))


def test_sin_parametros_devuelve_tupla_vacia() -> None:
    assert assert_params_allowed(None, path="/x") == ()


def test_la_url_se_compone_con_los_parametros() -> None:
    client, _ = make_client()
    url = client.build_url("/api/costos", (("desde", "2026-08-01"),))
    assert url == f"{BASE}/api/costos?desde=2026-08-01"


# --------------------------------------------------------------------------
# CA-06 — ningun secreto en repr ni en excepciones.
# --------------------------------------------------------------------------


def test_el_repr_de_la_llamada_no_muestra_valores_de_cabecera() -> None:
    """Una dataclass se imprime sola en trazas y logs: asi se filtra una cookie."""
    call = EndpointCall(
        method="GET",
        path=HEALTH_PATH,
        url=f"{BASE}{HEALTH_PATH}",
        headers=(("Cookie", COOKIE),),
    )
    texto = repr(call)
    assert COOKIE not in texto
    assert "Cookie" in texto  # el nombre si, para poder diagnosticar


def test_el_repr_de_la_respuesta_no_muestra_valores_de_cabecera() -> None:
    respuesta = HttpResponse(status=200, headers={"Set-Cookie": COOKIE}, body="{}")
    texto = repr(respuesta)
    assert COOKIE not in texto
    assert "set-cookie" in texto.lower()


def test_una_excepcion_de_error_no_arrastra_la_cookie() -> None:
    client, _ = make_client(SpyFetcher(HttpResponse(status=401, body="no autorizado")))
    with pytest.raises(TerminalAuthError) as exc:
        client.get(HEALTH_PATH, headers={"Cookie": COOKIE})
    assert COOKIE not in str(exc.value)


# --------------------------------------------------------------------------
# base_url y salud.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,esperado",
    [
        ("https://portal.example.com", "https://portal.example.com"),
        ("https://portal.example.com/", "https://portal.example.com"),
        ("https://portal.example.com//", "https://portal.example.com"),
    ],
)
def test_normalize_base_url_quita_barras_finales(raw: str, esperado: str) -> None:
    assert normalize_base_url(raw) == esperado


@pytest.mark.parametrize("raw", ["", "portal.example.com", "ftp://portal", "https://"])
def test_base_url_invalida_falla_cerrado(raw: str) -> None:
    with pytest.raises(PortalConfigError):
        normalize_base_url(raw)


def test_check_health_no_lanza_cuando_el_portal_esta_caido() -> None:
    """El pre-vuelo debe informar, no explotar: su respuesta decide si se
    intenta login, y una excepcion aqui saltaria esa decision."""
    client, _ = make_client(SpyFetcher(HttpResponse(status=503, body='{"ok":false,"db":"down"}')))
    resultado = check_health(client)
    assert resultado.ok is False
    assert resultado.status == 503


def test_check_health_reconoce_el_portal_sano() -> None:
    cuerpo = '{"ok":true,"db":"up","latencyMs":12}'
    client, _ = make_client(SpyFetcher(HttpResponse(status=200, body=cuerpo)))
    resultado = check_health(client)
    assert resultado.ok is True
    assert resultado.status == 200
