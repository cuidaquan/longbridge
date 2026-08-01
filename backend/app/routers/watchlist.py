from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..exceptions import LongbridgeAPIError
from ..services import (
    get_watchlist_quotes,
    get_watchlists,
    remove_watchlist_security,
    update_watchlist_pinned,
)


router = APIRouter(prefix="/watchlist", tags=["watchlist"])


class WatchlistPinPayload(BaseModel):
    is_pinned: bool


@router.get("")
def fetch_watchlists() -> dict[str, object]:
    try:
        groups = get_watchlists()
    except LongbridgeAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "groups": groups,
        "total_groups": len(groups),
        "total_securities": sum(len(group["securities"]) for group in groups),
    }


@router.get("/quotes")
def fetch_watchlist_quotes(
    symbols: str = Query(..., min_length=1, description="逗号分隔的证券代码"),
) -> dict[str, object]:
    symbol_list = list(dict.fromkeys(
        item.strip().upper()
        for item in symbols.split(",")
        if item.strip()
    ))
    if len(symbol_list) > 500:
        raise HTTPException(status_code=400, detail="一次最多查询 500 个标的")
    try:
        return {"quotes": get_watchlist_quotes(symbol_list)}
    except LongbridgeAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/securities/{symbol}")
def delete_watchlist_security(symbol: str) -> dict[str, object]:
    try:
        return remove_watchlist_security(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LongbridgeAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.put("/securities/{symbol}/pin")
def update_watchlist_pin(
    symbol: str,
    payload: WatchlistPinPayload,
) -> dict[str, object]:
    try:
        return update_watchlist_pinned(symbol, payload.is_pinned)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LongbridgeAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
