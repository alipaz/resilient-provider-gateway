from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/v1")


@router.post("/process")
async def process() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={
            "error": {
                "code": "not_implemented",
                "message": "The process endpoint is reserved for a later implementation step.",
            }
        },
    )
