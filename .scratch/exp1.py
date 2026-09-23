from typing import Annotated, Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.testclient import TestClient

app = FastAPI()


@app.post("/scalar-dup")
def scalar_dup(q: Annotated[str, Form()]):
    return {"q": q}


@app.post("/strict-int")
def strict_int(age: Annotated[int, Form(strict=True)]):
    return {"age": age}


@app.post("/opt-list")
def opt_list(tags: Annotated[Optional[list[str]], Form()] = None):
    return {"tags": tags}


@app.post("/tuple-set")
def tuple_set(
    t: Annotated[tuple[str, ...], Form()], s: Annotated[set[str], Form()]
):
    return {"t": t, "s": sorted(s)}


@app.post("/empty-file")
def empty_file(f: Annotated[UploadFile, File()]):
    return {"filename": f.filename, "size": f.size}


client = TestClient(app)

r = client.post("/scalar-dup", data=[("q", "a"), ("q", "b")])
print("scalar-dup:", r.status_code, r.json())
r = client.post("/strict-int", data={"age": "70"})
print("strict-int:", r.status_code, r.json())
print("opt-list missing:", client.post("/opt-list").json())
print("opt-list single:", client.post("/opt-list", data={"tags": "x"}).json())
print(
    "opt-list multi:",
    client.post("/opt-list", data=[("tags", "x"), ("tags", "y")]).json(),
)
print(
    "tuple-set:",
    client.post(
        "/tuple-set",
        data=[("t", "a"), ("t", "b"), ("s", "x"), ("s", "y"), ("s", "x")],
    ).json(),
)
r = client.post("/empty-file", files={"f": ("", b"")})
print("empty-file:", r.status_code, r.json())
r = client.post("/empty-file", files={"f": ("a.txt", b"")})
print("empty-content file:", r.status_code, r.json())
