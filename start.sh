#!/bin/bash

# Start Redis
redis-stack-server &

echo "Starting MCP Server..."

# Start MCP Server
python3 tool_mcp_server.py &

sleep 1

echo "Starting FastAPI..."

# Start Main App
exec uvicorn main:app --host 0.0.0.0 --port 8001 --reload