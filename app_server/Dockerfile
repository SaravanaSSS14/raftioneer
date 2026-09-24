FROM python:3.12-slim

WORKDIR /app

COPY app_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proto/ proto/
RUN python -m grpc_tools.protoc -I proto --python_out=. --grpc_python_out=. \
    proto/auction.proto proto/raft.proto proto/llm.proto

COPY app_server/ .

CMD ["python", "main.py"]
