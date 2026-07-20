FROM docker.io/library/python:3.11-slim

WORKDIR /app

# 安装 Python 依赖（你的依赖都是纯 Python，不需要 gcc）
COPY requirements.txt .
RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    -r requirements.txt

# 复制应用代码
COPY . .

# 创建数据目录
RUN mkdir -p /app/data /app/html

EXPOSE 10019
ENV PYTHONUNBUFFERED=1

# 启动应用
CMD ["uvicorn", "icutool_mail:app", "--host", "0.0.0.0", "--port", "10019"]