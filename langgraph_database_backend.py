"""
langgraph_database_backend.py
─────────────────────────────
LangChain create_agent + MCP (streamable_http) + AsyncSqliteSaver.

Requirements:
    pip install aiosqlite langchain-mcp-adapters langchain langchain-groq langgraph

Public API used by main.py:
    chatbot                  – compiled agent (ainvoke / aget_state)
    init_agent()             – call once at FastAPI startup
    shutdown_agent()         – call once at FastAPI shutdown
    retrieve_all_threads()   – async, returns list[str]
"""

import logging
import aiosqlite

from dotenv import load_dotenv

from langchain_groq import ChatGroq
from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain_core.messages import RemoveMessage
from prompt import main_prompt   # your existing system-prompt string

load_dotenv()
logger = logging.getLogger(__name__)

# ── singletons ────────────────────────────────
chatbot      = None
checkpointer = None
_db_conn     = None        # kept for checkpointer only (AsyncSqliteSaver needs one conn)
DB_PATH      = "chatbot.db"

from contextlib import asynccontextmanager

@asynccontextmanager
async def get_db_conn():
    """Fresh connection per label operation — safe for concurrent users."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn

MAX_QUERIES  = 5         # keep last 5 Q-A pairs
MAX_MESSAGES = MAX_QUERIES * 2   # = 10  (1 human + 1 AI per query)

# ── LLM factory ──────────────────────────────
def _load_llm() -> ChatGroq:
    return ChatGroq(model="openai/gpt-oss-20b")



async def trim_memory(config: dict) -> None:
    """
    After every agent invoke, keep only the last MAX_MESSAGES messages.

    Strategy (mirrors the notebook's cleanup_node):
      - If total > MAX_MESSAGES, compute excess
      - Round excess UP to even so we always remove complete Q-A pairs
        (never orphan a HumanMessage at the top of the buffer)
      - Call aupdate_state with RemoveMessage for each excess message
    """
    if chatbot is None:
        return

    state = await chatbot.aget_state(config)
    msgs  = state.values.get("messages", [])
    total = len(msgs)

    if total <= MAX_MESSAGES:
        return   # nothing to do

    excess = total - MAX_MESSAGES
    if excess % 2 != 0:   # always remove whole pairs
        excess += 1

    to_remove = msgs[:excess]   # oldest messages
    logger.debug(
        "[trim_memory] thread=%s | total=%d | removing=%d | keeping=%d",
        config["configurable"].get("thread_id"),
        total,
        len(to_remove),
        total - len(to_remove),
    )

    await chatbot.aupdate_state(
        config,
        {"messages": [RemoveMessage(id=m.id) for m in to_remove]},
    )




# ── startup ───────────────────────────────────
async def init_agent() -> None:
    global chatbot, checkpointer, _db_conn

    # 1. async SQLite
    logger.info("Opening async SQLite …")
    _db_conn     = await aiosqlite.connect("chatbot.db")
    checkpointer = AsyncSqliteSaver(conn=_db_conn)

    # 2. MCP tools
    logger.info("Fetching MCP tools …")
    client = MultiServerMCPClient(
        {
            "my_mcp": {
                "transport": "streamable_http",
                "url": "http://localhost:8004/mcp",
            }
        }
    )
    tools = await client.get_tools()
    logger.info("Tools loaded: %s", [t.name for t in tools])

    # 3. agent
    chatbot = create_agent(
        model=_load_llm(),
        tools=tools,
        system_prompt=main_prompt,  
        checkpointer=checkpointer,
        name="iprocess_agent",
    )
    logger.info("Agent ready.")


# ── shutdown ──────────────────────────────────
async def shutdown_agent() -> None:
    global _db_conn
    if _db_conn:
        await _db_conn.close()
        logger.info("DB connection closed.")


# ── thread listing ────────────────────────────
async def retrieve_all_threads() -> list[str]:
    if checkpointer is None:
        return []
    threads: set[str] = set()
    try:
        async for cp in checkpointer.alist(None):
            tid = cp.config["configurable"].get("thread_id")
            if tid:
                threads.add(str(tid))
    except Exception as e:
        logger.warning("retrieve_all_threads: %s", e)
    print(list(threads)[::-1],"-=-=-=-=-=-=-=-=-=-=-=")
    return list(threads)[::-1]


async def save_thread_label(thread_id: str, label: str, user_id: str = "default_user") -> None:
    async with get_db_conn() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS thread_labels (
                thread_id TEXT PRIMARY KEY,
                label     TEXT NOT NULL,
                user_id   TEXT NOT NULL DEFAULT 'default_user'
            )
        """)
        await conn.execute("""
            INSERT INTO thread_labels (thread_id, label, user_id)
            VALUES (?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET label = excluded.label, user_id = excluded.user_id
        """, (thread_id, label, user_id))
        await conn.commit()


async def get_thread_label(thread_id: str) -> str:
    async with get_db_conn() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS thread_labels (
                thread_id TEXT PRIMARY KEY,
                label     TEXT NOT NULL,
                user_id   TEXT NOT NULL DEFAULT 'default_user'
            )
        """)
        async with conn.execute(
            "SELECT label FROM thread_labels WHERE thread_id = ?", (thread_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else ""


async def get_user_threads(user_id: str) -> list[str]:
    async with get_db_conn() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS thread_labels (
                thread_id TEXT PRIMARY KEY,
                label     TEXT NOT NULL,
                user_id   TEXT NOT NULL DEFAULT 'default_user'
            )
        """)
        async with conn.execute(
            "SELECT thread_id FROM thread_labels WHERE user_id = ? ORDER BY rowid DESC",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]