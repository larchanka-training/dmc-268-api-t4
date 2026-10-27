import os

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import Response

router = APIRouter()
UPLOAD_DIR = "/var/app/uploads"




async def upload(name: str, file: UploadFile):
    with open(os.path.join(UPLOAD_DIR, name), "wb") as handle:
        handle.write(await file.read())
    return {"name": name}


@router.get("/files/{name}")
def download(name: str):
    try:
        with open(os.path.join(UPLOAD_DIR, name), "rb") as handle:
            return Response(handle.read())
    except FileNotFoundError:
        raise HTTPException(status_code=404) from None
