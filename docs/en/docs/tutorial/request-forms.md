# Form Data { #form-data }

When you need to receive form fields instead of JSON, you can use `Form`.

/// note

To use forms, first install [`python-multipart`](https://github.com/Kludex/python-multipart).

Add it to your project:

```console
$ uv add python-multipart
```

///

## Import `Form` { #import-form }

Import `Form` from `fastapi`:

{* ../../docs_src/request_forms/tutorial001_an_py310.py hl[3] *}

## Define `Form` parameters { #define-form-parameters }

Create form parameters the same way you would for `Body` or `Query`:

{* ../../docs_src/request_forms/tutorial001_an_py310.py hl[9] *}

For example, in one of the ways the OAuth2 specification can be used (called "password flow") it is required to send a `username` and `password` as form fields.

The <dfn title="specification">spec</dfn> requires the fields to be exactly named `username` and `password`, and to be sent as form fields, not JSON.

With `Form` you can declare the same configurations as with `Body` (and `Query`, `Path`, `Cookie`), including validation, examples, an alias (e.g. `user-name` instead of `username`), etc.

/// note

`Form` is a class that inherits directly from `Body`.

///

/// tip

To declare form bodies, you need to use `Form` explicitly, because without it the parameters would be interpreted as query parameters or body (JSON) parameters.

///

## About "Form Fields" { #about-form-fields }

The way HTML forms (`<form></form>`) send the data to the server normally uses a "special" encoding for that data, it's different from JSON.

**FastAPI** will make sure to read that data from the right place instead of JSON.

/// note | Technical Details

Data from forms is normally encoded using the "media type" `application/x-www-form-urlencoded`.

But when the form includes files, it is encoded as `multipart/form-data`. You'll read about handling files in the next chapter.

If you want to read more about these encodings and form fields, head to the [<abbr title="Mozilla Developer Network">MDN</abbr> web docs for `POST`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Methods/POST).

///

/// warning

You can declare multiple `Form` parameters in a *path operation*, but you can't also declare `Body` fields that you expect to receive as JSON, as the request will have the body encoded using `application/x-www-form-urlencoded` instead of `application/json`.

This is not a limitation of **FastAPI**, it's part of the HTTP protocol.

///

## Repeated Form Fields { #repeated-form-fields }

HTML forms can send the same field several times. If you declare the parameter
as a sequence (`list`, `set`, `tuple`, ...), every submitted value is kept, in
the order the client sent them:

```Python
@app.post("/items/")
async def create_items(tags: list[str] = Form()):
    return {"tags": tags}
```

If a single-value (scalar) field is sent several times, the last value is used
by default. Pass `strict=True` to reject such requests with a `422` response
instead:

```Python
@app.post("/items/")
async def create_item(name: str = Form(strict=True)):
    return {"name": name}
```

For form data, an empty string (and an empty file upload) is treated as if the
field was not sent, so parameters with a default value receive their default
(and `Optional` parameters receive `None`). These rules also apply to the
fields of Pydantic models declared with `Form()`, and the same semantics work
for `File()` parameters.

## Recap { #recap }

Use `Form` to declare form data input parameters.
