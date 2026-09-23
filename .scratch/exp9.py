import json

from tests.test_form_multivalue import app as app1
from tests.test_form_multivalue_sequences import app as app2

for app, names in [(app1, ["/lenient", "/strict"]), (app2, ["/sequences", "/files-strict"])]:
    spec = app.openapi()
    for path in names:
        rb = spec["paths"][path]["post"]["requestBody"]
        print(path, json.dumps(rb, indent=1)[:1200])
        print("---")
