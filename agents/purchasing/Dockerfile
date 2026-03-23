# Build from repo root: docker build -f agents/purchasing/Dockerfile .
# Pin digest: run `docker pull python:3.12-slim` and update the sha256 hash below.
FROM python:3.12-slim@sha256:fb1118f126b507965df3c46fdfc52312dfd5262e7b6652ef510bd9298f69a6bc

WORKDIR /app

COPY agents/purchasing/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy delegation library
COPY lib/ /opt/lib/

COPY agents/purchasing/ .

# Run as non-root user
RUN useradd --create-home appuser
USER appuser

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9000/')" || exit 1

CMD ["python", "main.py"]
