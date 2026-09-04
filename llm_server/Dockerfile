FROM python:3.12-slim

WORKDIR /app

COPY llm_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proto/ proto/
RUN python -m grpc_tools.protoc -I proto --python_out=. --grpc_python_out=. \
    proto/llm.proto

COPY llm_server/ .

CMD ["python", "main.py"]
