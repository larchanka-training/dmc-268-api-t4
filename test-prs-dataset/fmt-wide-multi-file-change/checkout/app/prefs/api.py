from fastapi import APIRouter

from app.prefs import service
from app.prefs.models import Preferences

router = APIRouter()


@router.get("/prefs/{user_id}")
def read(user_id: int):
    return service.load(user_id).__dict__


@router.put("/prefs/{user_id}")
def write(user_id: int, body: dict):
    prefs = Preferences(user_id, **body)
    service.save(prefs)
    return prefs.__dict__
