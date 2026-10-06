# Use a slim Python base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy source code into container
COPY . .

# Install OS dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install required Python packages
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    requests \
    prometheus-client \
    "opentelemetry-api>=1.26.0,<1.27.0" \
    "opentelemetry-sdk>=1.26.0,<1.27.0" \
    "opentelemetry-exporter-otlp-proto-grpc>=1.26.0,<1.27.0" \
    "opentelemetry-instrumentation-fastapi" \
    "opentelemetry-instrumentation-requests" \
    "opentelemetry-exporter-prometheus"

# Expose gateway API and Prometheus metrics ports
EXPOSE 8003
EXPOSE 8004

# Run FastAPI gateway via uvicorn
CMD ["uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "8003"]
