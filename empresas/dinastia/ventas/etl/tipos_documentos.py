#!/usr/bin/env python3
"""Dinastia — dimension de TIPOS DE DOCUMENTO: BD_BIABLE01 -> GCP.

Copia el catalogo `TIPOS_DOCUMENTOS` del ERP a `tipos_documentos_dinastia` en
Cloud SQL y le deriva una `clase` (FACTURA / NOTA_CREDITO / NOTA_DEBITO /
DEVOLUCION / OTRO) a partir de la DESCRIPCION que el propio ERP mantiene.

POR QUE UNA DIMENSION Y NO COLUMNAS EN LAS TABLAS DE HECHOS
-----------------------------------------------------------
`ventas_dinastia` y `margen_dinastia` YA llevan `id_tipdoc_fc` a GCP, asi que el
BI puede clasificar con un JOIN. Denormalizar la clase dentro de los hechos
obligaria a un `ALTER TABLE` sobre tablas de 2 GB (el loader solo hace
`CREATE TABLE IF NOT EXISTS`, nunca ALTER) y a recargar historia, sin ganar nada.

POR QUE LA CLASE SE DERIVA Y NO SE LISTA
----------------------------------------
El filtro de los ETLs es `ID_TIPDOC_FC NOT LIKE 'Z%'`: por exclusion, no por
lista blanca. Un prefijo nuevo entra solo — verificado en produccion, `FP` y `NZ`
aparecieron el 2026-06-26 y se cargaron sin tocar codigo. Si la clase saliera de
una lista mantenida a mano, ese prefijo nuevo llegaria SIN clasificar y romperia
la propiedad. Derivandola de la DESCRIPCION del catalogo, el ERP es la unica
fuente de verdad y lo nuevo queda clasificado solo.

Lo que no reconoce cae en `OTRO` — visible, nunca descartado en silencio. Ojo:
`VD` ("VENTAS DIARIAS", el asiento resumen del POS) cae en OTRO a proposito; NO
es una factura y sumarlo duplicaria toda la venta del punto de venta.

Uso:
    python etl/tipos_documentos.py            # refresca la dimension
    python etl/tipos_documentos.py --dry-run  # valida el cableado, no toca nada
"""
from __future__ import annotations

import argparse
import os
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

# Make `common` importable when run standalone (subprocess) or via the runner.
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.db import MySQLSource, build_source  # noqa: E402
from common.loader import GcpLoader, TargetSchema, WriteMode, build_loader  # noqa: E402
from common.utils import RECORDS_MARKER, PipelineConfig, get_module_logger  # noqa: E402

LOG = get_module_logger("etl_tipos_documentos")
DEFAULT_CONFIG = "/opt/dinastia-ventas/config/pipeline_config.yaml"

# La dimension NO comparte tabla con los hechos. El loader resuelve el nombre como
# `options['table'] or schema.table`, y en este deploy `options['table']` viene del
# config apuntando a la tabla de ventas — por eso se sobreescribe explicitamente
# en run(). Ver TARGET_TABLE_OVERRIDE.
TARGET_TABLE_OVERRIDE = "tipos_documentos_dinastia"

# El catalogo es chico (~130 filas) y cambia rara vez: se lee entero cada corrida.
SOURCE_SQL = """
SELECT
    TRIM(CODIGO)      AS codigo,
    TRIM(DESCRIPCION) AS descripcion,
    TRIM(MODULO)      AS modulo
FROM TIPOS_DOCUMENTOS
WHERE CODIGO IS NOT NULL AND TRIM(CODIGO) <> ''
ORDER BY CODIGO
"""

TARGET_SCHEMA = TargetSchema(
    table=TARGET_TABLE_OVERRIDE,
    columns={
        "codigo": "string",
        "descripcion": "string",
        "modulo": "string",
        "clase": "string",
        "fecha_carga": "timestamp",
    },
    primary_key=("codigo",),
)

# Clases emitidas. Se exponen para que las pruebas y el monitoreo no repitan literales.
CLASE_FACTURA = "FACTURA"
CLASE_NOTA_CREDITO = "NOTA_CREDITO"
CLASE_NOTA_DEBITO = "NOTA_DEBITO"
CLASE_DEVOLUCION = "DEVOLUCION"
CLASE_OTRO = "OTRO"


def _fold(text: str) -> str:
    """Mayusculas sin tildes, para que 'DEVOLUCION' y 'DEVOLUCIÓN' coincidan."""
    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in normalized if not unicodedata.combining(c)).upper()


def clasificar(descripcion: str) -> str:
    """Derivar la clase de documento desde la descripcion del catalogo del ERP.

    El orden importa: NOTA y DEVOLUCION se evaluan ANTES que FACTURA, porque una
    descripcion como "NOTA CREDITO ... FACTURA" debe quedar como nota, no factura.
    Lo no reconocido devuelve ``OTRO`` a proposito — mejor visible que mal
    clasificado (ej. `VD` = "VENTAS DIARIAS", que NO es una factura).

    ALCANCE — la clase dice QUE ES el documento, no de que lado del negocio.
    Comprobado contra el catalogo real (2026-08-05): acierta en los 8 codigos que
    llegan a los hechos de venta (FF/FL/FP/FR, NE/NX/NZ, ND) y en DV/VD. Pero
    algunos codigos que NUNCA aparecen ahi quedan aproximados: `FA` ("FACTURA DE
    ACREEDOR") y `NC` ("NOTA CREDITO ACREEDOR") son de COMPRAS y aun asi caen en
    FACTURA / NOTA_CREDITO; `AT` ("AJUSTE TEMPORAL POR FACTURA STANDAR") es un
    ajuste y cae en FACTURA; `DE`/`DP`/`DS` son devoluciones de PRESTAMO de
    mercancia, no de venta. Para separar venta de compra esta ``modulo``, que
    viaja en la misma dimension.
    """
    d = _fold(descripcion)
    if "NOTA CREDITO" in d or "NOTA DE CREDITO" in d:
        return CLASE_NOTA_CREDITO
    if "NOTA DEBITO" in d or "NOTA DE DEBITO" in d:
        return CLASE_NOTA_DEBITO
    if "DEVOLUCION" in d:
        return CLASE_DEVOLUCION
    if "FACTURA" in d:
        return CLASE_FACTURA
    return CLASE_OTRO


def extract(source: MySQLSource, batch_size: int = 1000) -> Iterable[list[dict]]:
    """Leer el catalogo completo del ERP (solo lectura)."""
    LOG.info("Leyendo TIPOS_DOCUMENTOS de %s", source.cfg.masked())
    yield from source.stream(SOURCE_SQL, (), batch_size=batch_size)


def transform(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Anadir la clase derivada y el sello de carga.

    ``codigo`` se deja tal cual (solo TRIM en el SQL): el catalogo distingue
    mayusculas — trae 'AJ' y 'Aj' como codigos distintos — y asi es como llega
    `id_tipdoc_fc` en las tablas de hechos. Normalizarlo aqui los colapsaria.
    """
    now = datetime.now()
    out: list[dict] = []
    for r in rows:
        descripcion = (r.get("descripcion") or "").strip()
        out.append(
            {
                "codigo": (r.get("codigo") or "").strip(),
                "descripcion": descripcion,
                "modulo": (r.get("modulo") or "").strip(),
                "clase": clasificar(descripcion),
                "fecha_carga": now,
            }
        )
    return out


def load(loader: GcpLoader, rows: list[dict]) -> int:
    """Crear la tabla si falta y hacer upsert por ``codigo``.

    ``ensure_target`` NO lo llama el loader solo — cada pipeline lo invoca en su
    propio ``load()`` (ver ``etl/ventas_rango.py``). Aqui importa doble: la tabla
    es nueva, asi que la primera corrida depende de este
    ``CREATE TABLE IF NOT EXISTS``, que ademas crea la PRIMARY KEY sobre
    ``codigo`` que el ``ON CONFLICT`` del upsert necesita.

    Upsert y no replace: lo nuevo entra, lo cambiado se actualiza, y los codigos
    que desaparezcan del ERP se conservan a proposito — los hechos historicos
    pueden seguir apuntando a ellos y perderiamos la etiqueta.
    """
    if not rows:
        return 0
    loader.ensure_target(TARGET_SCHEMA)
    return loader.load(rows, TARGET_SCHEMA, mode=WriteMode.UPSERT)


def run(dry_run: bool = False) -> int:
    config_path = _resolve_config_path()
    LOG.info("Config: %s", config_path)
    config = PipelineConfig(config_path)  # falla cerrado si falta un ${ENV}

    source = build_source(config)

    if dry_run:
        LOG.info("[DRY-RUN] source = %s", source.cfg.masked())
        LOG.info("[DRY-RUN] target = %s tabla=%s pk=%s",
                 config.get("target.type"), TARGET_SCHEMA.table,
                 ",".join(TARGET_SCHEMA.primary_key))
        LOG.info("[DRY-RUN] clases posibles: %s", ", ".join(
            (CLASE_FACTURA, CLASE_NOTA_CREDITO, CLASE_NOTA_DEBITO,
             CLASE_DEVOLUCION, CLASE_OTRO)))
        print(f"{RECORDS_MARKER} 0")
        return 0

    loader = build_loader(config)
    # El config de este deploy apunta a la tabla de ventas; la dimension va aparte.
    loader.options["table"] = TARGET_TABLE_OVERRIDE
    try:
        total = 0
        resumen: dict[str, int] = {}
        for batch in extract(source):
            rows = transform(batch)
            for row in rows:
                resumen[row["clase"]] = resumen.get(row["clase"], 0) + 1
            total += load(loader, rows)
        LOG.info("Cargados %d tipos -> %s", total, TARGET_TABLE_OVERRIDE)
        for clase in sorted(resumen):
            LOG.info("  %-13s %d", clase, resumen[clase])
        print(f"{RECORDS_MARKER} {total}")
        return 0
    finally:
        loader.close()


def _resolve_config_path() -> str:
    return os.environ.get("DINASTIA_ETL_CONFIG", DEFAULT_CONFIG)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="valida el cableado sin tocar el ERP ni GCP")
    args = parser.parse_args()
    try:
        return run(dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        LOG.error("Fallo el refresco de tipos de documento: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
