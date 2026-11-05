import re

import httpx
from fastapi import APIRouter

router = APIRouter()
TITLE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@router.get("/preview")
def preview(url: str):
    page = httpx.get(url, follow_redirects=True, timeout=5)
    match = TITLE.search(page.text)
    return {"title": match.group(1).strip() if match else None}
