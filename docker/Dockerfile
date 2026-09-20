FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy and install python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r /app/requirements.txt
# Round 8 (#5): preload the NLI model into the image cache so the first
# /v1/verify-claim in a fresh container is fast (no runtime download).
# Kept as its own layer so rebuilds cache it.
RUN python3 -c "from transformers import AutoTokenizer, AutoModelForSequenceClassification; AutoTokenizer.from_pretrained('cross-encoder/nli-deberta-v3-small'); AutoModelForSequenceClassification.from_pretrained('cross-encoder/nli-deberta-v3-small')"
# Tier 2 needs pytest in-image to run canonical suites in the subprocess sandbox
RUN pip install --no-cache-dir pytest

COPY daemon/ /app/

# Tier 2 executes the workspace's own test suite + manifest checks, which require git
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

ENV PORT=8000
EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
