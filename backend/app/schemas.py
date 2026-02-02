from datetime import datetime
from typing import Any, Dict, Optional, List
from pydantic import BaseModel

class HandoffContext(BaseModel):
    accountId: Optional[str] = None
    boardId: Optional[str] = None
    itemId: Optional[str] = None
    user: Optional[Dict[str, Any]] = None
    workspaceId: Optional[str] = None

class HandoffInitRequest(BaseModel):
    sessionToken: str
    context: HandoffContext

class HandoffInitResponse(BaseModel):
    url: str
    code: str

class HandoffResolveRequest(BaseModel):
    code: str

class HandoffResolveResponse(BaseModel):
    externalTaskKey: str

class TaskSyncRequest(BaseModel):
    force: bool = False
    runAsync: bool = False

class TaskSyncResponse(BaseModel):
    status: str
    snapshotVersion: Optional[str] = None

class TaskSummaryResponse(BaseModel):
    externalTaskKey: str
    snapshotVersion: Optional[str] = None
    taskContext: Optional[Dict[str, Any]] = None
    status: Optional[str] = None
    updatedAt: Optional[datetime] = None
    # Sync status for frontend polling
    syncStatus: Optional[str] = None  # idle | syncing | completed | failed
    syncStartedAt: Optional[datetime] = None
    syncCompletedAt: Optional[datetime] = None
    syncError: Optional[str] = None

class TaskSourceFile(BaseModel):
    id: str
    kind: str
    originalFilename: Optional[str] = None
    mimeType: Optional[str] = None
    sizeBytes: Optional[int] = None
    mondayAssetId: Optional[str] = None
    createdAt: Optional[datetime] = None

class TaskSourcesResponse(BaseModel):
    snapshotVersion: Optional[str] = None
    files: List[TaskSourceFile]

class SignedUrlResponse(BaseModel):
    url: str
    expiresAt: datetime

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    externalTaskKey: str
    message: str
    history: Optional[List[ChatMessage]] = None

class ChatStreamChunk(BaseModel):
    type: str
    content: Optional[str] = None
    citations: Optional[List[Dict[str, Any]]] = None