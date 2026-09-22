"""Unified raw/decoded semantics for FastAPI path parameters.

A single rule is applied consistently by route matching, Pydantic
validation and URL generation:

* Routes match against the **raw** (still percent-encoded) request path
  taken from ``scope["raw_path"]``, so an encoded slash (``%2F``) never
  changes segment boundaries.
* Each captured path parameter is percent-decoded **exactly once** with
  strict UTF-8 before convertors and Pydantic see it. Double-encoded
  input (e.g. ``%252F``) therefore decodes to the literal text ``%2F``
  and is never decoded a second time.
* A parameter whose decoded value is (or contains) a ``.`` or ``..``
  segment never matches, so ``%2E``/``%2E%2E`` cannot be used to escape
  the route hierarchy.
* Invalid UTF-8 in a matched parameter does not bypass constraints: it
  produces a 422 validation error with a stable location
  ``("path", <param name>)`` that documents the adopted
  decoded-exactly-once semantics.
* ``url_path_for()``/``url_for()`` re-encode parameters with the same
  canonical rules, so ``url_for(decoded_value)`` produces a raw URL that
  routes back to the very same decoded value (round-trip).

When a scope carries no ``raw_path`` (non-conforming ASGI servers), the
legacy behavior is kept as a fallback: matching happens on the already
decoded ``scope["path"]`` with no additional decoding step.
"""

import re
from typing import Any
from urllib.parse import quote, unquote

from starlette._utils import get_route_path
from starlette.convertors import Convertor, PathConvertor, StringConvertor
from starlette.types import Scope

# Scope key used to carry per-parameter decoding errors from route
# matching to the request/websocket handler, where they are raised as
# validation errors with a stable location.
PATH_PARAM_DECODE_ERRORS_KEY = "fastapi_path_param_decode_errors"

_PARAM_REGEX = re.compile(r"{([a-zA-Z_][a-zA-Z0-9_]*)(:[a-zA-Z_][a-zA-Z0-9_]*)?}")

# Characters that HTTP clients send literally in a path and that therefore
# stay unencoded both in compiled route patterns and generated URLs.
# Unreserved characters are never encoded by ``quote()``; sub-delims and
# ``:`` and ```` are valid literal path characters (RFC 3986).
_STATIC_SAFE = "/!$&'()*+,;=:@"
_PARAM_SAFE = "!$&'()*+,;=:@"


class PathParamDecodeError(ValueError):
    """A raw path parameter cannot be percent-decoded exactly once."""

    def __init__(self, raw_value: str) -> None:
        self.raw_value = raw_value
        super().__init__(f"Invalid percent-encoded path parameter: {raw_value!r}")


def decode_path_param(raw_value: str) -> str:
    """Percent-decode a single raw path parameter exactly once.

    Decoding is strict UTF-8: invalid percent-encoded bytes raise
    :class:``PathParamDecodeError`` instead of being silently replaced,
    so malformed input cannot bypass validation constraints.
    """
    try:
        decoded = unquote(raw_value, encoding="utf-8", errors="strict")
        # Reject lone surrogates coming from a non-conforming raw_path.
        decoded.encode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise PathParamDecodeError(raw_value) from exc
    return decoded


def encode_path_param(value: str, *, allow_slash: bool = False) -> str:
    """Canonically percent-encode a decoded path parameter value.

    This is the exact inverse of :func:``decode_path_param``: any character
    that is not valid literal path text is percent-encoded as UTF-8,
    including ``%`` itself (so an already-encoded-looking value such as
    ``%2F`` is generated as ``%252F`` and decodes back to ``%2F``).
    """
    safe = _PARAM_SAFE + "/" if allow_slash else _PARAM_SAFE
    return quote(value, safe=safe)


def has_dot_segment(decoded_value: str) -> bool:
    """Whether a decoded value is or contains a ``.``/``..`` path segment."""
    return any(segment in (".", "..") for segment in decoded_value.split("/"))


def get_raw_route_path(scope: Scope) -> str | None:
    """Return the raw (still percent-encoded) route path for matching.

    Mirrors ``starlette._utils.get_route_path`` but operates on
    ``scope["raw_path"]`` and strips a canonically encoded ``root_path``
    (as set by proxies or mounts). Returns ``None`` when the scope has no
    ``raw_path``, signaling the caller to fall back to the decoded path.
    """
    raw_path = scope.get("raw_path")
    if raw_path is None:
        return None
    if isinstance(raw_path, bytes):
        raw = raw_path.decode("utf-8", errors="surrogateescape")
    else:
        raw = str(raw_path)
    # Some servers include the query string in raw_path.
    raw = raw.split("?", 1)[0]
    root_path = scope.get("root_path", "")
    if not root_path:
        return raw
    for prefix in (quote(root_path, safe=_STATIC_SAFE), root_path):
        if not raw.startswith(prefix):
            continue
        if raw == prefix:
            return ""
        if raw[len(prefix)] == "/":
            return raw[len(prefix) :]
    return raw


def replace_raw_route_path(scope: Scope, new_route_path: str) -> bytes | None:
    """Rebuild ``scope["raw_path"]`` with a different route path.

    Used by the trailing-slash redirect so the re-match and the redirect
    target keep the same raw/decoded semantics (root_path prefix and
    query string are preserved untouched).
    """
    raw_path = scope.get("raw_path")
    if raw_path is None:
        return None
    if isinstance(raw_path, bytes):
        raw = raw_path.decode("utf-8", errors="surrogateescape")
    else:
        raw = str(raw_path)
    query = ""
    if "?" in raw:
        raw, query_string = raw.split("?", 1)
        query = "?" + query_string
    route_path = get_raw_route_path(scope)
    prefix = raw[: len(raw) - len(route_path)] if route_path else raw
    return (prefix + new_route_path + query).encode("utf-8", errors="surrogateescape")


def compile_raw_path_regex(
    path: str, param_convertors: dict[str, Convertor[Any]]
) -> re.Pattern[str]:
    """Compile a route path into a regex matching the raw (encoded) path.

    Static parts are canonically percent-encoded so a Unicode or reserved
    character in the route path matches the encoded form clients actually
    send. Parameter groups keep their convertor regex and therefore never
    cross a literal ``/`` unless the convertor allows it, which keeps
    ``%2F`` inside a single segment.
    """
    path_regex = "^"
    idx = 0
    for match in _PARAM_REGEX.finditer(path):
        param_name = match.group(1)
        path_regex += re.escape(quote(path[idx : match.start()], safe=_STATIC_SAFE))
        path_regex += f"(?P<{param_name}>{param_convertors[param_name].regex})"
        idx = match.end()
    path_regex += re.escape(quote(path[idx:], safe=_STATIC_SAFE)) + "$"
    return re.compile(path_regex)


def match_route_path_params(
    scope: Scope,
    *,
    path_regex: re.Pattern[str],
    raw_path_regex: re.Pattern[str] | None,
    param_convertors: dict[str, Convertor[Any]],
) -> tuple[bool, dict[str, Any], dict[str, str]]:
    """Match a scope against a route using the unified raw/decoded semantics.

    Returns ``(matched, path_params, decode_errors)``. ``decode_errors``
    maps parameter names to their undecodable raw values; the match still
    succeeds so the handler can report a 422 with a stable location
    instead of an opaque 404.
    """
    raw_route_path = get_raw_route_path(scope) if raw_path_regex is not None else None
    if raw_route_path is None or raw_path_regex is None:
        # Fallback for scopes without raw_path: legacy decoded matching.
        match = path_regex.match(get_route_path(scope))
        if not match:
            return False, {}, {}
        matched_params = match.groupdict()
        for key, value in matched_params.items():
            matched_params[key] = param_convertors[key].convert(value)
        return True, matched_params, {}
    match = raw_path_regex.match(raw_route_path)
    if not match:
        return False, {}, {}
    matched_params: dict[str, Any] = {}
    decode_errors: dict[str, str] = {}
    for key, raw_value in match.groupdict().items():
        try:
            decoded = decode_path_param(raw_value)
        except PathParamDecodeError:
            decode_errors[key] = raw_value
            matched_params[key] = raw_value
            continue
        if has_dot_segment(decoded):
            # A decoded "." or ".." segment must never match: it would let
            # %2E/%2E%2E escape the route hierarchy.
            return False, {}, {}
        matched_params[key] = param_convertors[key].convert(decoded)
    return True, matched_params, decode_errors


def _convertor_to_string(convertor: Convertor[Any], value: Any) -> str:
    if isinstance(convertor, StringConvertor):
        # Unlike Starlette decoded-path semantics, a "/" in the value is
        # fine here: it is encoded as %2F and decodes back within the same
        # segment. Only the empty string remains unusable.
        text = str(value)
        assert text, "Must not be empty"
        return text
    return convertor.to_string(value)


def replace_params_encoded(
    path_format: str,
    param_convertors: dict[str, Convertor[Any]],
    path_params: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Substitute parameters into a path format, encoding each value.

    Same contract as ``starlette.routing.replace_params`` but every value
    is canonically encoded with :func:``encode_path_param``, so generated
    URLs follow the same raw/decoded semantics as route matching.
    """
    remaining_params = dict(path_params)
    for key, value in list(remaining_params.items()):
        if "{" + key + "}" not in path_format:
            continue
        convertor = param_convertors[key]
        text = _convertor_to_string(convertor, value)
        allow_slash = isinstance(convertor, PathConvertor)
        path_format = path_format.replace(
            "{" + key + "}", encode_path_param(text, allow_slash=allow_slash)
        )
        remaining_params.pop(key)
    return path_format, remaining_params


def get_path_param_decode_errors(scope: Scope) -> list[dict[str, Any]]:
    """Build validation error dicts for undecodable matched path params.

    The location always uses the decoded-semantics ``("path", name)``
    form and the message states the adopted semantics explicitly.
    """
    raw_errors = scope.get(PATH_PARAM_DECODE_ERRORS_KEY) or {}
    return [
        {
            "type": "path_param_decode",
            "loc": ("path", name),
            "msg": (
                "Invalid percent-encoding in path parameter: value is not "
                "valid UTF-8. Path parameters are percent-decoded exactly "
                "once (strict UTF-8) before validation."
            ),
            "input": raw_value,
        }
        for name, raw_value in raw_errors.items()
    ]
