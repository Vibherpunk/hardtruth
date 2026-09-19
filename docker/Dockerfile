FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies for CPU ONNX & PyTorch
RUN pip install --no-cache-dir \
    fastapi==0.115.0 \
    uvicorn[standard]==0.31.0 \
    torch --index-url https://download.pytorch.org/whl/cpu \
    transformers==4.45.1 \
    sentence-transformers==3.1.1 \
    optimum[onnxruntime]==1.23.0

COPY daemon/app.py /app/app.py

ENV PORT=8000
EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
