# 使用国内镜像加速前缀拉取基础镜像，避免 Docker Hub 在国内被墙
ARG REGISTRY=docker.m.daocloud.io
FROM ${REGISTRY}/library/python:3.11-slim

WORKDIR /app

# Install system dependencies (needed for some Python packages)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 10019

# Run the application
CMD ["uvicorn", "icutool_mail", "app", "--host", "0.0.0.0", "--port", "10019", "--workers", "2"]