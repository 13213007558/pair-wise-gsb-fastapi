path = "docs/en/docs/tutorial/request-forms.md"
with open(path) as f:
    content = f.read()

section = """## Multiple Values and Strict Mode { #multiple-values-and-strict-mode }

HTML forms can send the same field multiple times (e.g. multi-select inputs). When you declare a field as a sequence (`list`, `set`, `tuple`), **FastAPI** collects all the repeated values, preserving their order:

```Python
@app.post("/items/")
async def create_items(tags: Annotated[list[str], Form()]):
    return {"tags": tags}
```

For a non-sequence field that receives multiple values, the last value is used by default. You can pass `strict=True` to `Form()` (or `File()`) to reject those requests with a `422` error instead:

```Python
@app.post("/items/")
async def create_item(name: Annotated[str, Form(strict=True)]):
    return {"name": name}
```

In strict mode, empty strings are also validated as-is instead of being treated as missing values. This also applies when using a Pydantic model with `Form(strict=True)`, the strict behavior is applied to all the fields of the model.

/// note | Technical Details

File parts with an empty filename (what browsers send when no file is selected) are treated as "no file", in both lenient and strict modes.

///

"""

anchor = "## Recap { #recap }"
assert anchor in content
content = content.replace(anchor, section + anchor, 1)
with open(path, "w") as f:
    f.write(content)
print("done")
