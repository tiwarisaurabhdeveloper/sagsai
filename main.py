"""
main.py  — SAGS AI
────────────────────────────────────────────────────────────
CHANGES FROM YOUR PREVIOUS VERSION (marked with  # ← NEW):
  1. Added  pathlib, shutil, List  imports
  2. Added  UploadFile, File, Form  from fastapi
  3. UPLOAD_DIR constant + auto-create on startup
  4. /chat now uses  Form + File  instead of JSON body
     so it can receive both text fields and file uploads
  5. Files are saved to uploads/ and their paths are
     appended to the user message sent to the agent
  6. Everything else (routes, _build_response, etc.)
     is identical to your working version
"""

import uuid, json, logging, ast, re
from contextlib import asynccontextmanager
from pathlib import Path                          # ← NEW
from typing import Optional, List                 # ← NEW  (added List)

from fastapi import FastAPI, HTTPException, UploadFile, File, Form   # ← NEW
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from langchain_core.messages import HumanMessage, AIMessage

import langgraph_database_backend as backend
from langgraph_database_backend import trim_memory

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── upload folder ─────────────────────────────  ← NEW
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)                  # create if not present
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

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
# NOTE: ChatRequest Pydantic model is REMOVED for /chat
# because that endpoint now uses Form fields (multipart).
# The other models stay exactly the same.

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


def _is_job_tool(name: str) -> bool:
    name = (name or "").lower()
    return "job" in name or "linkedin" in name


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
        logger.info("ToolMessage found: tool_call_id=%s  preview=%s", tc_id, str(msg.content)[:400])
        if tc_id not in job_tool_ids:
            continue
        parsed = _try_parse(msg.content)
        logger.info("Parsed: %s", str(parsed)[:400] if parsed else "None")
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


# ── routes ────────────────────────────────────

@app.get("/threads", response_model=ThreadListResponse)
async def get_threads():
    threads = await backend.retrieve_all_threads()
    return {"threads": threads}


@app.get("/threads/new", response_model=NewThreadResponse)
async def new_thread():
    return {"thread_id": str(uuid.uuid4())}


@app.get("/threads/{thread_id}/messages", response_model=ThreadMessagesResponse)
async def get_thread_messages(thread_id: str):
    if backend.chatbot is None:
        raise HTTPException(status_code=503, detail="Agent not ready.")
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


@app.post("/threads/{thread_id}/label")
async def set_thread_label(thread_id: str, payload: ThreadLabelRequest):
    await backend.save_thread_label(thread_id, payload.label)
    return {"ok": True}


@app.get("/threads/{thread_id}/label")
async def get_thread_label(thread_id: str):
    label = await backend.get_thread_label(thread_id)
    return {"label": label}

async def file_handling(files):
    saved_paths: list[str] = []
    for upload in files:
        if not upload.filename:
            continue
        print("-==--=-=-==-=-__dict__-=-=-=-=-=",upload.__dict__)
        if upload.content_type=='image/jpeg':  # this is the future development
            continue
    
        content = await upload.read()
        # enforce 10 MB limit
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"File '{upload.filename}' exceeds the 10 MB limit."
            )
        # save original file as-is to disk
        safe_name = f"{uuid.uuid4().hex}_{upload.filename}"
        dest = UPLOAD_DIR / safe_name
        with open(dest, "wb") as f:        # ← wb = write binary, saves exactly as original
            f.write(content)

        saved_paths.append(str(dest))
        logger.info("File saved: (%.2f KB)",  len(content) / 1024)
    return saved_paths
    

# ── /chat  (CHANGED: now accepts multipart/form-data) ─────────────────────────  ← NEW
@app.post("/chat")
async def chat(
    query:      str             = Form(...),           # ← NEW  text field
    thread_id:  str             = Form(...),           # ← NEW  text field
    web_search: bool            = Form(False),         # ← NEW  text field
    user_id:    str             = Form("default_user"),# ← NEW  text field
    files: List[UploadFile]     = File(default=[]),    # ← NEW  optional file list
):
    if backend.chatbot is None:
        raise HTTPException(status_code=503, detail="Agent not ready. Is MCP running?")

    # ── save uploaded files and build a list of saved paths ──────────────────  ← NEW
    saved_paths=await file_handling(files)
    # ── only pass file PATH to agent, not file content ──
    user_message = query
    if saved_paths:
        file_note = "\n\n[Attached files — give the summery of this of file to user and give the summry in the response if do not ask any things other hand if they ask any things from this file then give the response from this file ]\n" + "\n".join(
            f"- {p}" for p in saved_paths
        )
        user_message = query + file_note

    print(user_message)
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
    logger.info("web_search=%s  files=%d  response_type=%s",
                web_search, len(saved_paths), response.get("response_type"))
    return response