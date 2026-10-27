import requests
from fastapi import APIRouter

router = APIRouter()
UPSTREAM = "https://rates.example.com/v1/latest"


@router.get("/rates/{currency}")
async def rate(currency: str):
    response = requests.get(UPSTREAM, params={"base": currency}, timeout=10)
    response.raise_for_status()
    return response.json()
