"""FastAPI routes for the distribution lane. Mounted in app.py with one include_router line.
Errors are {"error": "<message>"} like the persona routes. See docs/DISTRIBUTION_API.md."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from .bundle import build_zip, export_run, load_export
from .specs import CHANNELS, DistributionError

router = APIRouter(prefix="/api/distribution", tags=["distribution"])


def _err(exc):
    return JSONResponse({"error": str(exc)}, status_code=getattr(exc, "status", 400))


async def _body(request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return None
    return body if isinstance(body, dict) else None


def _root(request):
    return getattr(request.app.state, "runs_root", None)


@router.get("/channels")
def distribution_channels():
    return {"channels": list(CHANNELS.values())}


@router.post("/export")
async def distribution_export(request: Request):
    body = await _body(request)
    if body is None:
        return JSONResponse({"error": "JSON object required"}, status_code=400)
    try:
        return export_run(
            body.get("run"), variant_id=body.get("variant_id"), channels=body.get("channels"),
            mode=body.get("mode") or "auto", render=body.get("render", True) is not False, root=_root(request),
        )
    except DistributionError as exc:
        return _err(exc)


@router.get("/export/{run}")
def distribution_get_export(run: str, request: Request, export_id: str | None = None):
    try:
        return load_export(run, export_id, root=_root(request))
    except DistributionError as exc:
        return _err(exc)


@router.get("/bundle/{run}/{export_id}.zip")
def distribution_bundle(run: str, export_id: str, request: Request, channel: str | None = None):
    try:
        path = build_zip(run, export_id, channel, root=_root(request))
    except DistributionError as exc:
        return _err(exc)
    return FileResponse(path, media_type="application/zip", filename=path.name)
