FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY app.py mcp_server.py ./
COPY samples ./samples
COPY .streamlit ./.streamlit
RUN pip install --no-cache-dir -e . "mcp>=2"
EXPOSE 8501 8600 8765
# default: dashboard; override the command for the MCP server (see docker-compose.yml)
CMD ["streamlit", "run", "app.py", "--server.address", "0.0.0.0", "--server.port", "8501"]
