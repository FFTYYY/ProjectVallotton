FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8766

# MCP_HOST is forced to 0.0.0.0 by docker-compose (the host port mapping
# stays loopback-bound, so this is not an exposure).
CMD ["python", "server.py"]
