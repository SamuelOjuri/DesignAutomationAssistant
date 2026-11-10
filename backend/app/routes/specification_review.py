"""Direct, item-scoped Specification Review API, independent of task ingestion."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..services import specification_review_auth as review_auth
from ..services import specification_review_workspace as workspace
from ..services.performance_specification_sources import (
    MondaySpecificationGateway, SpecificationIneligible, SpecificationSourceError,
    SpecificationSourceWaiting,
)
from ..services.specification_review import ApproveReview, ReviewConflict
from ..services.specification_review_contract import SaveReviewDecisions


# Explicit seams also let route tests exercise permissions independently of Monday.
bootstrap_review_session = review_auth.bootstrap_review_session
authenticate_review = review_auth.authenticate_review

PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _error_status(code: str) -> int:
    if code == "authorization_expired":
        return 401
    if code == "reviewer_forbidden":
        return 403
    if code == "source_waiting":
        return 202
    if code == "source_unavailable":
        return 503
    if code == "publication_failed":
        return 502
    return 409


def _error(code, message, *, status=None):
    return JSONResponse(
        status_code=status or _error_status(code),
        content={"detail": {"code": code, "message": message}},
        headers=PRIVATE_HEADERS,
    )


class ReviewRoute(APIRoute):
    """Keep private responses and tokens out of caches and validation echoes."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request):
            try:
                response = await original(request)
            except ReviewConflict as exc:
                return _error(exc.code, str(exc))
            except SpecificationSourceWaiting:
                return _error("source_waiting", "Waiting for current source analysis")
            except SpecificationIneligible:
                return _error("scope_changed", "The item is no longer eligible for editing")
            except SpecificationSourceError:
                return _error("source_unavailable", "Current recommendations are unavailable")
            except RequestValidationError as exc:
                # Pydantic's default errors include the rejected input, which may
                # contain the SDK token. Return locations/messages only.
                return JSONResponse(
                    status_code=422,
                    content={"detail": {
                        "code": "invalid_request", "message": "Invalid review request",
                        "errors": [{"loc": list(error["loc"]), "type": error["type"],
                                    "message": error["msg"]} for error in exc.errors()],
                    }}, headers=PRIVATE_HEADERS,
                )
            except HTTPException as exc:
                if isinstance(exc.detail, dict) and "code" in exc.detail:
                    response = JSONResponse(
                        status_code=exc.status_code, content={"detail": exc.detail},
                        headers=exc.headers,
                    )
                else:
                    # Upstream errors can contain request bodies or transport
                    # URLs. Only the explicit review contract crosses this API.
                    code = ("authorization_expired" if exc.status_code == 401 else
                            "reviewer_forbidden" if exc.status_code == 403 else
                            "source_unavailable")
                    response = _error(code, "Review request could not be completed",
                                      status=exc.status_code)
            response.headers.update(PRIVATE_HEADERS)
            return response

        return handle


router = APIRouter(
    prefix="/api/specification-review", tags=["specification-review"],
    route_class=ReviewRoute,
)


def _gateway():
    token = settings.monday_ingestion_access_token
    if not token:
        raise ReviewConflict("source_unavailable", "Specification source access is not configured")
    return MondaySpecificationGateway(token)


def _access(db, authorization, board_id, item_id, *, capability="read"):
    access = authenticate_review(db, authorization, board_id, item_id)
    if not access.capabilities.get("read"):
        raise ReviewConflict("reviewer_forbidden", "No access to this specification")
    if not access.capabilities.get(capability):
        if access.scope_reason in {
            "outside_landing_zone", "completed_folder", "board_not_managed",
            "item_archived", "item_deleted", "item_unknown",
        }:
            raise ReviewConflict("scope_changed", "The item is outside the editable Landing Zone")
        raise ReviewConflict("reviewer_forbidden", "This review is currently read-only")
    return access


@router.post("/session")
def session(payload: review_auth.SessionBootstrap, db: Session = Depends(get_db)):
    return bootstrap_review_session(db, payload)


@router.get("/items/{board_id}/{item_id}")
def review_workspace(board_id: str, item_id: str, authorization: str | None = Header(None),
                     db: Session = Depends(get_db)):
    access = _access(db, authorization, board_id, item_id)
    editable = bool(access.capabilities.get("edit"))
    result = workspace.get_workspace(
        db, board_id=board_id, item_id=item_id, account_id=access.account_id,
        gateway=_gateway() if editable else None, editable=editable,
    )
    return {**result, "capabilities": access.capabilities, "scope_reason": access.scope_reason}


@router.put("/items/{board_id}/{item_id}/decisions")
def decisions(board_id: str, item_id: str, payload: SaveReviewDecisions,
              authorization: str | None = Header(None), db: Session = Depends(get_db)):
    access = _access(db, authorization, board_id, item_id, capability="edit")
    return workspace.save_decisions(
        db, board_id=board_id, item_id=item_id, actor=access.actor,
        command=payload, gateway=_gateway(),
    )


@router.post("/items/{board_id}/{item_id}/refresh")
def refresh(board_id: str, item_id: str, payload: workspace.RefreshReview,
            authorization: str | None = Header(None), db: Session = Depends(get_db)):
    access = _access(db, authorization, board_id, item_id, capability="edit")
    return workspace.refresh_recommendations(
        db, board_id=board_id, item_id=item_id, actor=access.actor,
        command=payload, gateway=_gateway(),
    )


@router.get("/items/{board_id}/{item_id}/candidates")
def candidates(board_id: str, item_id: str, authorization: str | None = Header(None),
               db: Session = Depends(get_db)):
    access = _access(db, authorization, board_id, item_id)
    editable = bool(access.capabilities.get("edit"))
    result = workspace.get_workspace(
        db, board_id=board_id, item_id=item_id, account_id=access.account_id,
        gateway=_gateway() if editable else None, editable=editable,
    )
    return {"candidates": result.get("candidates"),
            "review_version": result.get("review_version"),
            "source_fingerprint": result.get("source_fingerprint")}


@router.get("/items/{board_id}/{item_id}/status")
def status(board_id: str, item_id: str, authorization: str | None = Header(None),
           db: Session = Depends(get_db)):
    access = _access(db, authorization, board_id, item_id)
    result = workspace.get_status(
        db, board_id=board_id, item_id=item_id, account_id=access.account_id,
        editable=bool(access.capabilities.get("edit")),
    )
    return {**result, "capabilities": access.capabilities, "scope_reason": access.scope_reason}


@router.get("/items/{board_id}/{item_id}/pdf/{revision}")
def pdf(board_id: str, item_id: str, revision: str,
        authorization: str | None = Header(None), db: Session = Depends(get_db)):
    access = _access(db, authorization, board_id, item_id)
    filename, content = workspace.read_pdf(
        db, board_id=board_id, item_id=item_id, account_id=access.account_id,
        revision=revision,
    )
    # The ledger name is server-authored, but still reject header metacharacters.
    safe_name = "".join(c for c in filename if c.isascii() and (c.isalnum() or c in "._-"))
    return Response(content, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="{safe_name or "specification.pdf"}"',
        **PRIVATE_HEADERS,
    })


@router.post("/items/{board_id}/{item_id}/approve")
def approve(board_id: str, item_id: str, payload: ApproveReview,
            authorization: str | None = Header(None), db: Session = Depends(get_db)):
    access = _access(db, authorization, board_id, item_id, capability="approve")
    return workspace.approve_specification(
        db, board_id=board_id, item_id=item_id, actor=access.actor,
        command=payload, gateway=_gateway(),
    )
