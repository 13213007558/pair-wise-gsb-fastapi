from typing import Annotated, Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.testclient import TestClient

app = FastAPI()


@app.post("/scalar-dup")
def scalar_dup(q: Annotated[str, Form()]):
    return {"q": q}


@app.post("/opt-list")
def opt_list(tags: Annotated[Optional[list[str]], Form()] = None):
    return {"tags": tags}


@app.post("/tuple-set")
def tuple_set(
    t: Annotated[tuple[str, ...], Form()], s: Annotated[set[str], Form()]
):
    return {"t": t, "s": sorted(s)}


@app.post("/empty-str")
def empty_str(name: Annotated[str, Form()], age: Annotated[Optional[int], Form()] = None):
    return {"name": name, "age": age}


@app.post("/files")
def files(f: Annotated[list[UploadFile], File()]):
    return [{"filename": x.filename, "size": x.size} for x in f]


client = TestClient(app)

r = client.post("/scalar-dup", data={"q": ["a", "b"]})
print("scalar-dup:", r.status_code, r.json())
print("opt-list missing:", client.post("/opt-list").json())
print("opt-list single:", client.post("/opt-list", data={"tags": "x"}).json())
print("opt-list multi:", client.post("/opt-list", data={"tags": ["x", "y"]}).json())
print(
    "tuple-set:",
    client.post(
        "/tuple-set", data={"t": ["a", "b"], "s": ["x", "y", "x"]}
    ).json(),
)
r = client.post("/empty-str", data={"name": "", "age": ""})
print("empty-str:", r.status_code, r.json())
r = client.post("/files", files=[("f", ("a.txt", b"1")), ("f", ("", b""))])
print("files+empty:", r.status_code, r.json())
