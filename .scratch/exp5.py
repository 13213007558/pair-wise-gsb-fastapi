from typing import Annotated

from fastapi import Form
from fastapi._compat.v2 import ModelField, asdict

fi = Form(strict=True, annotation=int)
print("field_info strict attr:", getattr(fi, "strict", "<missing>"))
d = asdict(fi)
print("asdict attributes:", d["attributes"])
mf = ModelField(field_info=fi, name="age")
print("validate '70':", mf.validate("70", {}, loc=("body", "age")))
print("validate 70:", mf.validate(70, {}, loc=("body", "age")))
