path = "fastapi/param_functions.py"
with open(path) as f:
    lines = f.readlines()

new_doc = (
    "    strict: Annotated[\n"
    "        bool | None,\n"
    "        Doc(\n"
    "            \"\"\"\n"
    "            If `True`, use strict form parsing: multiple values for a\n"
    "            non-sequence field are rejected with a 422 error instead of\n"
    "            silently using the last one, and empty strings are validated\n"
    "            as-is instead of being treated as missing values.\n"
    "            \"\"\"\n"
    "        ),\n"
    "    ] = _Unset,\n"
)

# Form strict block: lines 1816-1823 (1-based); File strict block: 2131-2138
assert lines[1815].startswith("    strict: Annotated["), lines[1815]
assert lines[1822].startswith("    ] = _Unset,"), lines[1822]
assert lines[2130].startswith("    strict: Annotated["), lines[2130]
assert lines[2137].startswith("    ] = _Unset,"), lines[2137]

lines[2130:2138] = [new_doc]
lines[1815:1823] = [new_doc]
with open(path, "w") as f:
    f.writelines(lines)
print("done")
