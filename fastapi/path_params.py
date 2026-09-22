"""
Unified raw/decoded semantics for FastAPI path parameters.

This module is the single source of truth for how path parameters move
between their raw (percent-encoded) and decoded forms:

* Route matching happens against the *raw* request path
  (scope["raw_path"]), so an encoded slash ("%2F") can never change
  segment boundaries, and literal "/" characters are the only segment
  delimiters.
* Each captured parameter is decoded exactly once, with strict validation:
  invalid percent-escapes and invalid UTF-8 are rejected instead of being
  silently replaced, and decoded "." / ".." segments are rejected so
  "%2E" / "%2E%2E" cannot escape the route tree. Decoding never recurses,
  so "%252F" decodes to the literal value "%2F" and nothing more.
* url_path_for() applies the inverse rule: values are encoded exactly
  once with the same character classes, so generated URLs round-trip
  through the matcher back to the same decoded values.
* When the raw and decoded forms of a value cannot both satisfy the
  constraints (e.g. a decoded ".." segment), the raised/recorded error
  states which semantics were applied.
"""

import re
import urllib.parse
from typing import Any, NamedTuple

from starlette._utils import get_route_path
from starlette.convertors import CONVERTOR_TYPES, PathConvertor, StringConvertor
from starlette.routing import PARAM_REGEX
from starlette.types import Scope

# Scope keys used to expose the raw side of the evidence chain: the raw
# (still percent-encoded) parameter values and any decode errors recorded
# while matching the winning route.
RAW_PATH_PARAMS_SCOPE_KEY = "fastapi_raw_path_params"
PATH_PARAM_ERRORS_SCOPE_KEY = "fastapi_path_param_errors"

# Characters that may appear unencoded in a raw URL path (RFC 3986
# unreserved + sub-delims + ":" / "@" / "/"), plus "%" for existing escapes.
_RAW_SAFE_CHARS = "/%!$&'()*+,;=:@-._~"

# Same set without "%": used when computing the raw (wire) form of route
# literals and root_path prefixes, where a literal "%" must itself be
# encoded as "%25".
_RAW_LITERAL_SAFE_CHARS = "/!$&'()*+,;=:@-._~"

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

_DOT_SEGMENTS = (".", "..")

# Semantics labels used in error messages to state which form of the value
# a constraint was checked against.
RAW_SEMANTICS = "raw"
DECODED_SEMANTICS = "decoded"


class PathParamDecodeError(ValueError):
    """A raw path parameter cannot be decoded or violates path semantics."""

    def __init__(self, message: str, *, semantics: str) -> None:
        super().__init__(message)
        self.semantics = semantics


class PathParamEncodeError(ValueError):
    """A decoded path parameter value cannot be encoded into a URL."""


class PathMatchResult(NamedTuple):
    path_params: dict[str, Any]
    raw_path_params: dict[str, str]
    errors: dict[str, PathParamDecodeError]


def _validate_percent_encoding(raw_value: str, *, param_name: str) -> None:
    idx = 0
    while True:
        idx = raw_value.find("%", idx)
        if idx == -1:
            return
        if (
            idx + 2 >= len(raw_value)
            or raw_value[idx + 1] not in _HEX_DIGITS
            or raw_value[idx + 2] not in _HEX_DIGITS
        ):
            raise PathParamDecodeError(
                f"Invalid percent-encoding in raw path parameter '{param_name}': "
                f"'%' at position {idx} is not followed by two hexadecimal "
                "digits (checked against the raw, still-encoded value)",
                semantics=RAW_SEMANTICS,
            )
        idx += 3


def validate_decoded_path_param(value: str, *, param_name: str) -> None:
    """Reject decoded values that cannot safely satisfy path semantics."""
    segments = value.split("/")
    for segment in segments:
        if segment in _DOT_SEGMENTS:
            raise PathParamDecodeError(
                f"Path parameter '{param_name}' decodes to a '{segment}' "
                "path segment, which is not allowed as it could escape the "
                "route tree (checked against the decoded value after a "
                "single percent-decoding pass)",
                semantics=DECODED_SEMANTICS,
            )


def decode_path_param(raw_value: str, *, param_name: str) -> str:
    """Decode one raw path parameter exactly once, with strict validation.

    Raises PathParamDecodeError for invalid percent-escapes, invalid UTF-8,
    and decoded "." / ".." segments.
    """
    _validate_percent_encoding(raw_value, param_name=param_name)
    raw_bytes = urllib.parse.unquote_to_bytes(raw_value)
    try:
        decoded = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PathParamDecodeError(
            f"Invalid UTF-8 in percent-encoded path parameter '{param_name}': "
            f"{exc} (checked against the raw, still-encoded value)",
            semantics=RAW_SEMANTICS,
        ) from exc
    validate_decoded_path_param(decoded, param_name=param_name)
    return decoded


def encode_path_param(value: str, *, param_name: str, allow_slash: bool) -> str:
    """Encode a decoded path parameter value for URL generation.

    This is the exact inverse of decode_path_param(): encoding happens once,
    so a literal "%" becomes "%25" and (for non-"path" parameters) "/" becomes
    "%2F". Values containing "." / ".." segments cannot be represented in a
    URL without being normalized away by clients and servers, so they are
    rejected with the same rule applied when decoding.
    """
    segments = value.split("/")
    for segment in segments:
        if segment in _DOT_SEGMENTS:
            raise PathParamEncodeError(
                f"Cannot generate a URL for path parameter '{param_name}': "
                f"the value contains a '{segment}' segment, which is not "
                "allowed by the decoded path parameter semantics (it would "
                "be normalized away or rejected when decoded)"
            )
    return urllib.parse.quote(value, safe="/" if allow_slash else "")


def get_full_raw_path(scope: Scope) -> str | None:
    """Return the normalized full raw request path, or None if unavailable.

    Existing "%XX" escapes are kept untouched (decoding happens exactly
    once, later); any other non-ASCII bytes are percent-encoded.
    """
    raw_path = scope.get("raw_path")
    if raw_path is None:
        return None
    raw_bytes = bytes(raw_path).split(b"?", 1)[0]
    return urllib.parse.quote_from_bytes(raw_bytes, safe=_RAW_SAFE_CHARS)


def get_raw_route_path(scope: Scope) -> tuple[str, bool]:
    """Return the route path to match against and whether it is raw.

    The second element is True when the returned path is the raw
    (percent-encoded) path and parameters still need to be decoded, False
    when only the legacy decoded path is available (e.g. a server that does
    not provide "raw_path", or a mount whose prefix cannot be mapped onto
    the raw path), in which case parameters are used as-is.
    """
    raw = get_full_raw_path(scope)
    if raw is None:
        return get_route_path(scope), False
    root_path = scope.get("root_path", "")
    if not root_path:
        return raw, True
    raw_root = urllib.parse.quote(root_path, safe=_RAW_LITERAL_SAFE_CHARS)
    if raw == raw_root:
        return "", True
    if raw.startswith(raw_root):
        return raw[len(raw_root) :], True
    decoded_path = scope.get("path", "")
    if isinstance(decoded_path, str) and decoded_path.startswith(root_path):
        # The decoded path carries the root_path prefix but the raw path
        # does not (e.g. a Mount with encoded characters in its prefix).
        # Fall back to the legacy decoded semantics for this request.
        return get_route_path(scope), False
    # The root_path is not part of the request path at all (e.g. a test
    # client or proxy that only uses root_path for URL generation).
    return raw, True


def compile_raw_path_regex(path: str) -> "re.Pattern[str]":
    """Compile a route path template into a regex for the raw request path.

    Literal parts are percent-encoded so non-ASCII route paths match their
    encoded wire form; parameter groups keep the convertor regexes, which
    are all ASCII-safe and therefore match raw segments unchanged.
    """
    path_regex = "^"
    idx = 0
    for match in PARAM_REGEX.finditer(path):
        param_name, convertor_type = match.groups("str")
        convertor_type = convertor_type.lstrip(":")
        convertor = CONVERTOR_TYPES[convertor_type]
        literal = urllib.parse.quote(
            path[idx : match.start()], safe=_RAW_LITERAL_SAFE_CHARS
        )
        path_regex += re.escape(literal)
        path_regex += f"(?P<{param_name}>{convertor.regex})"
        idx = match.end()
    literal = urllib.parse.quote(path[idx:], safe=_RAW_LITERAL_SAFE_CHARS)
    path_regex += re.escape(literal) + "$"
    return re.compile(path_regex)


def match_path_params(
    *,
    raw_path_regex: "re.Pattern[str]",
    path_regex: "re.Pattern[str]",
    param_convertors: dict[str, Any],
    scope: Scope,
) -> PathMatchResult | None:
    """Match a route against the scope, decoding parameters exactly once.

    Returns None when the path does not match. Otherwise returns the decoded
    parameters, their raw counterparts, and any decode errors keyed by
    parameter name. Decode errors do not prevent the match: they are
    reported through the normal validation error pipeline so that error
    locations stay stable (["path", <name>]).
    """
    route_path, is_raw = get_raw_route_path(scope)
    active_regex = raw_path_regex if is_raw else path_regex
    match = active_regex.match(route_path)
    if match is None:
        return None
    raw_params = match.groupdict()
    path_params: dict[str, Any] = {}
    errors: dict[str, PathParamDecodeError] = {}
    for key, raw_value in raw_params.items():
        convertor = param_convertors[key]
        decoded = raw_value
        if is_raw:
            try:
                decoded = decode_path_param(raw_value, param_name=key)
            except PathParamDecodeError as exc:
                errors[key] = exc
                decoded = raw_value
        else:
            # The value was already decoded by the server; the decoded
            # semantics still apply.
            try:
                validate_decoded_path_param(decoded, param_name=key)
            except PathParamDecodeError as exc:
                errors[key] = exc
        try:
            path_params[key] = convertor.convert(decoded)
        except (ValueError, AssertionError) as exc:
            errors[key] = PathParamDecodeError(
                f"Path parameter '{key}' with value {decoded!r} could not be "
                f"converted after decoding: {exc} (checked against the "
                "decoded value after a single percent-decoding pass)",
                semantics=DECODED_SEMANTICS,
            )
            path_params[key] = decoded
    return PathMatchResult(
        path_params=path_params,
        raw_path_params=raw_params,
        errors=errors,
    )


def replace_path_params(
    path_format: str,
    param_convertors: dict[str, Any],
    path_params: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Substitute parameters into a path format, encoding each exactly once.

    This mirrors starlette.routing.replace_params() but percent-encodes the
    converted values with the inverse of the decoding rules, so generated
    URLs round-trip through route matching back to the same decoded values.
    """
    for key, value in list(path_params.items()):
        if "{" + key + "}" not in path_format:
            continue
        convertor = param_convertors[key]
        allow_slash = isinstance(convertor, PathConvertor)
        if isinstance(convertor, StringConvertor) and not allow_slash:
            # StringConvertor.to_string() rejects "/" in values, but with
            # raw-semantics matching an encoded slash ("%2F") is
            # representable, so only the emptiness check applies.
            text = str(value)
            if not text:
                raise PathParamEncodeError(
                    f"Cannot generate a URL for path parameter '{key}': "
                    "the value must not be empty"
                )
        else:
            text = convertor.to_string(value)
        encoded = encode_path_param(text, param_name=key, allow_slash=allow_slash)
        path_format = path_format.replace("{" + key + "}", encoded)
        path_params.pop(key)
    return path_format, path_params


def build_path_param_error(
    *, param_name: str, raw_value: Any, error: PathParamDecodeError
) -> dict[str, Any]:
    """Build a validation error dict for a path parameter decode failure.

    The location always starts with "path" so it is stable across parameter
    types, and the message states whether the raw or the decoded semantics
    were applied.
    """
    return {
        "type": "path_param_decode",
        "loc": ("path", param_name),
        "msg": str(error),
        "input": raw_value,
        "ctx": {"semantics": error.semantics},
    }
