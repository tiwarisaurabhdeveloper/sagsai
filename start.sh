# #!/bin/bash

# # Start Redis
# redis-stack-server &

# echo "Starting MCP Server..."

# # Start MCP Server
# python3 tool_mcp_server.py &

# sleep 1

# echo "Starting FastAPI..."

# # Start Main App
# exec uvicorn main:app --host 0.0.0.0 --port 8001 --reload




#!/bin/bash

echo "Checking Redis Stack installation..."

# Check if redis-stack-server exists
if ! command -v redis-stack-server &> /dev/null
then
    echo "Redis Stack not found. Installing..."

    # Install Redis Stack
    brew tap redis-stack/redis-stack
    brew install redis-stack

    echo "Redis Stack installed successfully"
else
    echo "Redis Stack already installed"
fi

# =========================
# START REDIS
# =========================
echo "Starting Redis..."
redis-stack-server &

sleep 2

# =========================
# START MCP SERVER
# =========================
echo "Starting MCP Server..."
python3 tool_mcp_server.py &

sleep 1

# =========================
# START FASTAPI
# =========================
echo "Starting FastAPI..."

exec uvicorn main:app \
    --host 0.0.0.0 \
    --port 8001 \
    --reload