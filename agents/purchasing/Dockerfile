# Build from repo root: docker build -f agents/purchasing/Dockerfile .
FROM python:3.12-slim

WORKDIR /app

COPY agents/purchasing/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy delegation library
COPY lib/ /opt/lib/

COPY agents/purchasing/ .

EXPOSE 9000

CMD ["python", "main.py"]
