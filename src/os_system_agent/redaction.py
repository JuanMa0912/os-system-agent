"""Secret redaction helpers.

Pure, dependency-free functions applied before any text is logged, reported,
or sent to a channel. Principle #1: no secrets in logs, reports, prompts, or
notifications. This is a safety net, not a substitute for keeping secrets out
of the data in the first place.

Two properties the pipeline must hold, because callers depend on them:

* **Idempotent** — ``redact(redact(x)) == redact(x)``. Spec 006 (§RNF-04) redacts
  on write *and* on read, so already-masked text goes through here a second
  time; a second pass must be a no-op, never a mask of a mask.
* **Linear-ish** — every quantifier is either bounded, possessive, or separated
  from the next one by a mandatory literal. A redactor that hangs on hostile
  input is a redactor that gets removed from the hot path.

Over-redaction is the accepted trade: a masked value that was not a secret
costs readability, an unmasked one costs a credential.
"""

from __future__ import annotations

import re
from collections.abc import Callable

REDACTED = "***REDACTED***"

# --------------------------------------------------------------------------
# Shapes that are a secret on their own.
# --------------------------------------------------------------------------

# PEM blocks. The whole block is replaced, body included. The END marker is
# optional on purpose: a log truncated mid-key still carries key material, so
# `\Z` closes the match at end-of-text instead of leaving the tail exposed.
# `[A-Z0-9 ]{0,40}` covers RSA / EC / OPENSSH / ENCRYPTED variants.
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----"
    r"[\s\S]*?"
    r"(?:-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----|\Z)"
)
# El marcador NO reconstruye una cabecera PEM: si lo hiciera, gitleaks y
# leak_guard.sh marcarian la propia salida del redactor como una llave.
_PEM_PLACEHOLDER = f"[PEM PRIVATE KEY {REDACTED}]"

# JWT: three base64url segments. Anchored on the `ey` that every JWT header
# starts with (the header is JSON and base64url of `{"` is `eyJ`). Sin ese
# ancla, un `tabla.columna.sufijo` o una ruta con puntos se redactaria por
# error, y un reporte ilegible tambien es un fallo operativo.
# `++` / `*+` son posesivos: si no viene el punto, el motor falla de una vez en
# vez de retroceder. Sin retroceso no hay explosion exponencial.
_JWT = re.compile(r"\bey[A-Za-z0-9_-]{6,}+\.[A-Za-z0-9_-]++\.[A-Za-z0-9_-]*+")

_TELEGRAM_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")
_PROVIDER_KEY = re.compile(
    r"\b(?:sk-[A-Za-z0-9]{20,}"
    r"|gh[posur]_[A-Za-z0-9]{20,}"
    r"|AIza[A-Za-z0-9_-]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,})\b"
)

# Password inside a connection URI: scheme://user:PASSWORD@host. Runs before the
# e-mail rule so the host survives and only the password is lost.
_URI_PASSWORD = re.compile(r"(://[^:@/\s]+:)[^@/\s]+(@)")

# --------------------------------------------------------------------------
# Headers whose whole value is a secret.
# --------------------------------------------------------------------------

# `Cookie:` / `Set-Cookie:` — the entire header value is masked, not each
# `name=value` pair: una cookie es una lista separada por `;`, y enmascarar solo
# el primer par dejaria el resto a la vista. Se corta en fin de linea o en la
# comilla que cierra el valor, para no comerse el resto de un log JSON de una
# sola linea.
_COOKIE_HEADER = re.compile(r"(?i)\b((?:set-)?cookie)([\"']?[ \t]*[=:][ \t]*[\"']?)([^\r\n\"']*)")

# Authorization-style headers: the scheme (`Basic`, `Bearer`) is part of the
# value, so masking to end of value is the only safe option.
_AUTH_HEADER = re.compile(
    r"(?i)\b((?:proxy-)?authorization|x-api-key|x-auth-token|x-csrf-token)"
    r"([\"']?[ \t]*[=:][ \t]*[\"']?)([^\r\n\"']*)"
)

# A bare `Bearer <token>` outside a header (curl transcripts, error bodies).
_BEARER_SCHEME = re.compile(r"(?i)\b(bearer)([ \t]+)[A-Za-z0-9._~+/=-]+")

# --------------------------------------------------------------------------
# key = value / "key": "value" where the key name implies a secret.
# --------------------------------------------------------------------------

# Palabras que hacen secreto al valor de la derecha. `session` entra porque las
# cookies del portal (`vp_session`) aparecen tambien sueltas en logs; `key` a
# secas NO entra, porque en este repo `key:` es casi siempre una clave de
# diccionario inofensiva y redactarla arruinaria los reportes.
# (La excepcion a S105 de bandit para este archivo esta en pyproject.toml, con su
# justificacion: aqui no hay ningun secreto, solo los nombres de campo que el
# redactor busca para taparlos.)
_SECRET_WORD = (
    r"(?:token|secret|passw(?:or)?d|pwd|credential"
    r"|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|authorization|bearer|csrf|cookie|session)"
)

# Los `{0,32}` alrededor de la palabra clave estan acotados a proposito: el
# motor prueba a lo sumo 33 posiciones por arranque en vez de recorrer la
# cadena entera, y el coste queda lineal.
#
# El grupo 2 se traga la comilla que CIERRA la clave en JSON (`"password":`).
# Ese era el fallo de la v1: el patron exigia el separador pegado a la palabra,
# asi que `{"password": "x"}` no se redactaba en absoluto.
#
# El grupo 3 es `(["'])?` — con el `?` FUERA del grupo — para que el condicional
# `(?(3)...)` sepa distinguir "hay comilla" de "no hay comilla". Con comilla el
# valor llega hasta la comilla de cierre (asi se cubren valores con espacios);
# sin comilla, hasta el primer separador.
_KV_SECRET = re.compile(
    r"(?i)"
    r"([A-Za-z0-9_.\-]{0,32}" + _SECRET_WORD + r"[A-Za-z0-9_.\-]{0,32})"
    r"([\"']?[ \t]*[=:][ \t]*)"
    r"([\"'])?"
    r"(?(3)[^\"'\r\n]*|[^\s\"',;]++)"
)

# --------------------------------------------------------------------------
# Data that is not a credential but must not travel either.
# --------------------------------------------------------------------------

# E-mail addresses (spec 006 §9 keeps nominal personal data out of scope, and an
# address is nominal). Local part bounded to the RFC 5321 maximum of 64 so a
# long run of non-address text cannot make this quadratic; the domain is split
# into labels so no quantifier competes with another over the same dots.
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]{1,64}+@[A-Za-z0-9\-]{1,63}+(?:\.[A-Za-z0-9\-]{1,63}+){1,8}")

# RFC 1918 addresses: internal network topology is exactly what leak_guard.sh
# exists to keep out of this repo, and it must not leave through a report either.
_PRIVATE_IP = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)


def _mask_kv(match: re.Match[str]) -> str:
    """Keep the key, the separator and the opening quote; drop the value."""
    return f"{match.group(1)}{match.group(2)}{match.group(3) or ''}{REDACTED}"


# Order matters: PEM first (its body would otherwise be chewed by the key/value
# rules), then the URI password before the e-mail rule so `user:pass@host` keeps
# its host. Every entry is a fixed point — applying it to its own output returns
# the same text — which is what makes :func:`redact` idempotent.
_Replacement = str | Callable[[re.Match[str]], str]

_PIPELINE: tuple[tuple[re.Pattern[str], _Replacement], ...] = (
    (_PEM_BLOCK, _PEM_PLACEHOLDER),
    (_JWT, REDACTED),
    (_TELEGRAM_TOKEN, REDACTED),
    (_PROVIDER_KEY, REDACTED),
    (_URI_PASSWORD, rf"\1{REDACTED}\2"),
    (_COOKIE_HEADER, rf"\1\2{REDACTED}"),
    (_AUTH_HEADER, rf"\1\2{REDACTED}"),
    (_BEARER_SCHEME, rf"\1\2{REDACTED}"),
    (_KV_SECRET, _mask_kv),
    (_EMAIL, REDACTED),
    (_PRIVATE_IP, REDACTED),
)


def redact(text: str) -> str:
    """Return ``text`` with known secret shapes masked.

    Masks: PEM private keys, JWTs, Telegram bot tokens, common provider keys,
    passwords inside connection URIs, ``Cookie``/``Set-Cookie``/authorization
    headers, secret-looking ``key=value`` and ``"key": "value"`` pairs, e-mail
    addresses and RFC 1918 addresses.

    The result is a fixed point: passing it back in returns it unchanged.
    """
    if not text:
        return text
    out = text
    for pattern, replacement in _PIPELINE:
        out = pattern.sub(replacement, out)
    return out
