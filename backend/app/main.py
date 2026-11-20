# backend/app/main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.app.config import settings
from backend.app.db import keep_idle_connections
from backend.app.routes.monday_handoff import router as monday_handoff_router
from backend.app.routes.monday_auth import router as monday_auth_router
from backend.app.routes.monday_webhooks import router as monday_webhooks_router
from backend.app.routes.tasks import router as tasks_router
from backend.app.routes.chat import router as chat_router
from backend.app.routes.specification_review import router as specification_review_router
from backend.app.services.crm_accounts import start_directory_warmup

import logging
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_app):
    keep_idle_connections()
    # Company Account matching needs the CRM Accounts directory; load it in the
    # background so startup and the first review do not wait for Monday.
    if settings.performance_specification_review_enabled and settings.monday_ingestion_access_token:
        start_directory_warmup(settings.monday_ingestion_access_token)
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Location"],
)

app.include_router(monday_handoff_router)
app.include_router(monday_auth_router)
app.include_router(monday_webhooks_router)
app.include_router(tasks_router)
app.include_router(chat_router)
app.include_router(specification_review_router)
