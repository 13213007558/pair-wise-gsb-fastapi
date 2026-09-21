# Serializer Context { #serializer-context }

When several tenants share an application, they often need different JSON
encodings for the same data: one tenant may want `Decimal` values as
strings with two decimal places while another wants floats, and UUIDs may
need different formats.

You can pass a `response_serializer` to a *path operation* (or to an
`APIRouter` as a default) to give the `response_model` serialization an
explicit, request-scoped encoder context, without modifying any global
state.

## `SerializerContext` { #serializercontext }

A `SerializerContext` is an immutable container of per-type encoders that
are applied on top of FastAPI's default JSON encoders when serializing the
response of a single request:

* It only affects the response of the **current request**. It is resolved
  per request and passed explicitly down the serialization path, so
  concurrent requests, async endpoints and background tasks never share
  temporary encoder state.
* It does **not** affect the OpenAPI schema, which is derived from
  `response_model` only.
* `response_model_include`, `response_model_exclude`,
  `response_model_by_alias` and friends keep working independently.
* Response validation still runs first: validation failures raise
  `ResponseValidationError` with the original `ValidationError` locations
  and context, and unserializable objects raise an error instead of being
  silently converted to strings during JSON encoding.

## Selecting an encoder per tenant { #selecting-an-encoder-per-tenant }

Set `response_serializer` to a callable receiving the current `Request`
and returning the `SerializerContext` to use for that response (or
`None` to use the default encoding):

{* ../../docs_src/serializer_context/tutorial001.py hl[19:39] *}

The context encoders apply recursively to nested models, lists, dictionaries
and `Union` branches. `None` values pass through untouched.

You can also pass a `SerializerContext` directly (instead of a callable)
to apply it to every response of the *path operation*, or set
`response_serializer` on an `APIRouter` as a router-level default that
individual *path operations* can override.

## Bypass rules { #bypass-rules }

The serializer context is only used when FastAPI serializes the response
for you. It is **not** applied when:

* The endpoint returns a `Response` directly (including
  `StreamingResponse` and custom `Response` subclasses): the response is
  sent as-is.
* The endpoint is a generator streaming JSONL or Server-Sent Events: the
  streamed items use the default encoders.
