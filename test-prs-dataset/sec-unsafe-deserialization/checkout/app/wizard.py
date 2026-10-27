import base64
import pickle

from fastapi import APIRouter

router = APIRouter()


@router.post("/wizard/start")
def start():
    return {"step": 1, "answers": {}}


@router.post("/wizard/resume")
def resume(body: dict):
    state = pickle.loads(base64.b64decode(body["state"]))
    return {"step": state["step"] + 1, "answers": state["answers"]}
