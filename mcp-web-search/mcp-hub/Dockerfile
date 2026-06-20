FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    ddgs \
    mcp \
    httpx \
    markdownify \
    trafilatura==2.1.0 \
    pydantic-settings \
    uvicorn

COPY src/ /app/

EXPOSE 8765

CMD ["python", "hub.py"]
