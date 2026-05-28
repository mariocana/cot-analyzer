FROM python:3.11-slim

WORKDIR /app

# System deps for lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Single worker: job state is in-process memory.
# ANTHROPIC_API_KEY should be passed at runtime: docker run -e ANTHROPIC_API_KEY=...
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:8000", "app:app", "--timeout", "120"]
