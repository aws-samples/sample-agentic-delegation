# Build from repo root: docker build -f agents/pricing/Dockerfile .
FROM python:3.12-slim

WORKDIR /app

COPY agents/pricing/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy delegation library
COPY lib/ /opt/lib/

COPY agents/pricing/ .

EXPOSE 9000

CMD ["python", "main.py"]
