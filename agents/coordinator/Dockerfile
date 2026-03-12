# Build from repo root: docker build -f agents/coordinator/Dockerfile .
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY agents/coordinator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy delegation library
COPY lib/ /opt/lib/

# Copy agent code
COPY agents/coordinator/ .

EXPOSE 9000

CMD ["python", "main.py"]
