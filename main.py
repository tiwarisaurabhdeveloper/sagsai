"""
main.py
───────
FastAPI chat server.

Run:
    uvicorn main:app --reload --port 8001
    (your MCP server must be running on port 8004)
"""

import uuid, json, logging, ast, re
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles

from langchain_core.messages import HumanMessage, AIMessage

import langgraph_database_backend as backend
from langgraph_database_backend import trim_memory

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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
class ChatRequest(BaseModel):
    query: str
    thread_id: str
    web_search: bool
    user_id: Optional[str] = "default_user"

class NewThreadResponse(BaseModel):
    thread_id: str

class ThreadListResponse(BaseModel):
    threads: list[str]

class ThreadMessagesResponse(BaseModel):
    messages: list[dict]


# ── helpers ───────────────────────────────────

def _try_parse(content) -> Optional[dict]:
    """
    Try every serialisation format LangGraph / MCP might produce.
    Returns a dict if successful, else None.
    """
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

    # 1. Standard JSON
    try:
        result = json.loads(content)
        if isinstance(result, dict):
            return result
    except Exception:
        pass

    # 2. Python literal — handles single-quoted dicts from str(dict)
    try:
        result = ast.literal_eval(content)
        if isinstance(result, dict):
            return result
    except Exception:
        pass

    # 3. JSON/dict buried inside a larger string
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

    # find where current turn starts (after last HumanMessage)
    last_human_idx = None
    for i in reversed(range(len(messages))):
        if getattr(messages[i], "type", "") == "human":
            last_human_idx = i
            break
    recent = messages[last_human_idx + 1:] if last_human_idx is not None else messages

    # collect every tool-call id that looks like a job search
    job_tool_ids: set[str] = set()
    for msg in recent:
        if getattr(msg, "type", "") == "ai":
            for tc in getattr(msg, "tool_calls", []):
                name = tc.get("name", "")
                logger.info("Tool call detected: name=%s  id=%s", name, tc.get("id"))
                if _is_job_tool(name):
                    job_tool_ids.add(tc.get("id"))

    logger.info("Job tool IDs: %s", job_tool_ids)

    # scan every ToolMessage and log it so you can debug
    for msg in recent:
        if getattr(msg, "type", "") != "tool":
            continue

        tc_id = getattr(msg, "tool_call_id", "")
        logger.info(
            "ToolMessage found: tool_call_id=%s  preview=%s",
            tc_id, str(msg.content)[:400],
        )

        if tc_id not in job_tool_ids:
            continue  # not a job tool result — skip

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


# ── add this model ──
class ThreadLabelRequest(BaseModel):
    label: str

@app.post("/threads/{thread_id}/label")
async def set_thread_label(thread_id: str, payload: ThreadLabelRequest):
    await backend.save_thread_label(thread_id, payload.label)
    return {"ok": True}

@app.get("/threads/{thread_id}/label")
async def get_thread_label(thread_id: str):
    label = await backend.get_thread_label(thread_id)
    return {"label": label}


@app.post("/chat")
async def chat(payload: ChatRequest):
    if backend.chatbot is None:
        raise HTTPException(status_code=503, detail="Agent not ready. Is MCP running?")

    config = {"configurable": {"thread_id": payload.thread_id}}

    try:
        result = await backend.chatbot.ainvoke(
            {"messages": [{"role": "user", "content": payload.query}]},
            config,
        )
        await trim_memory(config)
        if not result["messages"][-1].content:
            result["messages"][-1].content = "I'm sorry, I couldn't generate a response."
    except Exception as e:
        logger.exception("Agent invocation error")
        raise HTTPException(status_code=500, detail=str(e))

    response = _build_response(result, payload.thread_id)
    logger.info("Response type returned: %s", response.get("response_type"))
    return response