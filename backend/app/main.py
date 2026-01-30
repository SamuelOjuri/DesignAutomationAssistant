# backend/app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.app.routes.monday_handoff import router as monday_handoff_router
from backend.app.routes.monday_auth import router as monday_auth_router
from backend.app.routes.tasks import router as tasks_router
from backend.app.routes.chat import router as chat_router

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten for prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Location"],
)

app.include_router(monday_handoff_router)
app.include_router(monday_auth_router)
app.include_router(tasks_router)
app.include_router(chat_router)