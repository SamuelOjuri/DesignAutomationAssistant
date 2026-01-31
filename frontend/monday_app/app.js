/* global mondaySdk */
const monday = mondaySdk();

const config = window.APP_CONFIG || {};
const apiBaseUrl = (config.apiBaseUrl || "").replace(/\/$/, "");

const state = {
  context: null,
  sessionToken: null,
};

const elements = {
  status: document.getElementById("status"),
  accountId: document.getElementById("accountId"),
  boardId: document.getElementById("boardId"),
  itemId: document.getElementById("itemId"),
  userId: document.getElementById("userId"),
  openBtn: document.getElementById("openBtn"),
  error: document.getElementById("error"),
};

function setStatus(message) {
  elements.status.textContent = message;
}

function setError(message) {
  elements.error.textContent = message || "";
}

function setMeta(context) {
  elements.accountId.textContent = context.accountId || "-";
  elements.boardId.textContent = context.boardId || "-";
  elements.itemId.textContent = context.itemId || "-";
  elements.userId.textContent = (context.user && context.user.id) || "-";
}

async function loadContext() {
  setStatus("Fetching monday context...");
  setError("");

  const [contextResult, tokenResult] = await Promise.all([
    monday.get("context"),
    monday.get("sessionToken"),
  ]);

  state.context = contextResult.data;
  state.sessionToken = tokenResult.data;

  setMeta(state.context);
  setStatus("Ready to open the item in the target app.");
  elements.openBtn.disabled = false;
}

async function requestHandoff() {
  if (!state.context || !state.sessionToken) {
    throw new Error("Missing monday context or session token.");
  }

  const payload = {
    sessionToken: state.sessionToken,
    context: {
      accountId: String(state.context.accountId),
      boardId: String(state.context.boardId),
      itemId: String(state.context.itemId),
      user: state.context.user,
      workspaceId: state.context.workspaceId != null ? String(state.context.workspaceId) : null,
    },
  };

  const response = await fetch(`${apiBaseUrl}/api/monday/handoff/init`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(
      `Handoff init failed (${response.status}). ${body || "No error details."}`
    );
  }

  return response.json();
}

async function handleOpenClick() {
  elements.openBtn.disabled = true;
  setError("");
  setStatus("Requesting handoff...");

  try {
    const result = await requestHandoff();
    if (!result || !result.url) {
      throw new Error("Backend did not return a redirect URL.");
    }

    await monday.execute("openLinkInTab", { url: result.url });
    setStatus("Opening the item in the target app...");
  } catch (error) {
    setStatus("Unable to open the item.");
    setError(error.message);
    elements.openBtn.disabled = false;
  }
}

elements.openBtn.addEventListener("click", handleOpenClick);

loadContext().catch((error) => {
  setStatus("Unable to load monday context.");
  setError(error.message);
});
