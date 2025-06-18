// Use official Python image with a stable version
FROM python:3.11-slim

// Install build tools and Rust
RUN apt-get update && apt-get install -y \
    build-essential \
    pkg-config \
    libssl-dev \
    curl \
    rustc

// Set working directory
WORKDIR /app

// Copy code
COPY . .

// Install dependencies
RUN pip install --upgrade pip && pip install -r requirements.txt

// Expose the port Render provides
EXPOSE 10000

// Start the FastAPI app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]
