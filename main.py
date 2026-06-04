"""
main.py  — SAGS AI
"""

import uuid, json, logging, ast, re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from langchain_core.messages import HumanMessage, AIMessage

import langgraph_database_backend as backend
from langgraph_database_backend import trim_memory
from langgraph_database_backend import get_user_threads

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── upload folder ─────────────────────────────
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
MAX_FILE_SIZE = 5 * 1024 * 1024  # 10 MB


# ── lifespan ──────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await backend.init_agent()
    except Exception as e:
        logger.error("Agent init failed: %s", e)
    yield
    await backend.shutdown_agent()


app = FastAPI(title="SAGS AI", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="statics"), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── UI ────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


# ── models ────────────────────────────────────
class NewThreadResponse(BaseModel):
    thread_id: str

class ThreadListResponse(BaseModel):
    threads: list[str]

class ThreadMessagesResponse(BaseModel):
    messages: list[dict]

class ThreadLabelRequest(BaseModel):
    label: str


# ── helpers ───────────────────────────────────

def _try_parse(content) -> Optional[dict]:
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        try:
            content = str(content)
        except Exception:
            return None
    content = content.strip()
    if not content:
        return None
    try:
        result = json.loads(content)
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    try:
        result = ast.literal_eval(content)
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    match = re.search(r'\{.*\}', content, re.DOTALL)
    if match:
        for parser in (json.loads, ast.literal_eval):
            try:
                result = parser(match.group())
                if isinstance(result, dict):
                    return result
            except Exception:
                pass
    return None


JOB_TOOLS = {"linkedin_job_search", "file_job_search"}

def _is_job_tool(name: str) -> bool:
    return (name or "").lower() in JOB_TOOLS


def _build_response(result: dict, thread_id: str) -> dict:
    messages = result.get("messages", [])
    ai_text = messages[-1].content if messages else ""

    last_human_idx = None
    for i in reversed(range(len(messages))):
        if getattr(messages[i], "type", "") == "human":
            last_human_idx = i
            break
    recent = messages[last_human_idx + 1:] if last_human_idx is not None else messages

    job_tool_ids: set[str] = set()
    for msg in recent:
        if getattr(msg, "type", "") == "ai":
            for tc in getattr(msg, "tool_calls", []):
                name = tc.get("name", "")
                logger.info("Tool call detected: name=%s  id=%s", name, tc.get("id"))
                if _is_job_tool(name):
                    job_tool_ids.add(tc.get("id"))

    logger.info("Job tool IDs: %s", job_tool_ids)

    for msg in recent:
        if getattr(msg, "type", "") != "tool":
            continue
        tc_id = getattr(msg, "tool_call_id", "")
        # logger.info("ToolMessage found: tool_call_id=%s  preview=%s", tc_id, str(msg.content)[:400])
        if tc_id not in job_tool_ids:
            continue
        parsed = _try_parse(msg.content)
        # logger.info("Parsed: %s", str(parsed)[:400] if parsed else "None")
        if parsed and parsed.get("type") == "jobs" and parsed.get("jobs"):
            logger.info("SUCCESS: %d job cards", len(parsed["jobs"]))
            return {
                "response_type": "jobs",
                "jobs": parsed["jobs"],
                "text": ai_text,
                "thread_id": thread_id,
            }

    logger.info("FALLBACK: returning plain text")
    return {"response_type": "text", "answer": ai_text, "thread_id": thread_id}


# ── file handling ─────────────────────────────
async def file_handling(files) -> list[str]:
    """
    Skips images, saves all other files to uploads/.
    Replaces if same filename already exists.
    Returns list of saved filenames.
    """
    saved_filenames: list[str] = []

    for upload in files:
        if not upload.filename:
            continue

        # skip images
        if upload.content_type and upload.content_type.startswith("image/"):
            logger.info("Skipping image: %s", upload.filename)
            continue

        content = await upload.read()

        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"File '{upload.filename}' exceeds the 10 MB limit."
            )

        # save — overwrites automatically if same filename exists
        dest = UPLOAD_DIR / upload.filename
        with open(dest, "wb") as f:
            f.write(content)

        saved_filenames.append(upload.filename)
        logger.info("Saved/Replaced: %s (%.2f KB)", dest, len(content) / 1024)

    return saved_filenames


# ── routes ────────────────────────────────────
# add this import at the top with other imports
from langgraph_database_backend import get_user_threads

# ── GET /threads — now scoped to user ─────────
@app.get("/threads", response_model=ThreadListResponse)
async def get_threads(user_id: str = "default_user"):
    threads = await get_user_threads(user_id)
    return {"threads": threads}


# ── GET /threads/new — same, no change needed ──
@app.get("/threads/new", response_model=NewThreadResponse)
async def new_thread():
    return {"thread_id": str(uuid.uuid4())}


# ── GET /threads/{thread_id}/messages ─────────
@app.get("/threads/{thread_id}/messages", response_model=ThreadMessagesResponse)
async def get_thread_messages(thread_id: str, user_id: str = "default_user"):
    if backend.chatbot is None:
        raise HTTPException(status_code=503, detail="Agent not ready.")

    # security check — only owner can read their thread
    owner = await _get_thread_owner(thread_id)
    if owner and owner != user_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    try:
        state = await backend.chatbot.aget_state(
            config={"configurable": {"thread_id": thread_id}}
        )
        raw = state.values.get("messages", [])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    messages = []
    for msg in raw:
        if isinstance(msg, HumanMessage) and msg.content:
            messages.append({"role": "user", "content": msg.content})
        elif isinstance(msg, AIMessage) and msg.content:
            messages.append({"role": "assistant", "content": msg.content})
    return {"messages": messages}


# ── POST /threads/{thread_id}/label ───────────
@app.post("/threads/{thread_id}/label")
async def set_thread_label(thread_id: str, payload: ThreadLabelRequest, user_id: str = "default_user"):
    await backend.save_thread_label(thread_id, payload.label, user_id)
    return {"ok": True}


# ── GET /threads/{thread_id}/label ────────────
@app.get("/threads/{thread_id}/label")
async def get_thread_label(thread_id: str):
    label = await backend.get_thread_label(thread_id)
    return {"label": label}


# ── /chat — pass user_id through ──────────────
@app.post("/chat")
async def chat(
    query:      str           = Form(...),
    thread_id:  str           = Form(...),
    web_search: bool          = Form(False),
    user_id:    str           = Form("default_user"),   # ← already exists, no change
    files: List[UploadFile]   = File(default=[]),
):
    if backend.chatbot is None:
        raise HTTPException(status_code=503, detail="Agent not ready. Is MCP running?")

    saved_filenames = await file_handling(files)

    user_message = query
    if saved_filenames:
        user_message = (
            f"{query}\n\n"
            f"[UPLOADED FILES: {', '.join(saved_filenames)}]\n"
            f"Use these filenames when calling any file-related tools."
        )

    logger.info("Agent message preview:\n%s", user_message[:400])

    config = {"configurable": {"thread_id": thread_id}}
    try:
        result = await backend.chatbot.ainvoke(
            {"messages": [{"role": "user", "content": user_message}]},
            config,
        )
        await trim_memory(config)
        if not result["messages"][-1].content:
            result["messages"][-1].content = "I'm sorry, I couldn't generate a response."
    except Exception as e:
        logger.exception("Agent invocation error")
        raise HTTPException(status_code=500, detail=str(e))

    response = _build_response(result, thread_id)
    logger.info("web_search=%s  files=%s  response_type=%s  user=%s",
                web_search, saved_filenames, response.get("response_type"), user_id)
    return response


# ── helper — get thread owner ─────────────────
async def _get_thread_owner(thread_id: str) -> str | None:
    async with backend.get_db_conn() as conn:
        async with conn.execute(
            "SELECT user_id FROM thread_labels WHERE thread_id = ?", (thread_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None