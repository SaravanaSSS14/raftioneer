FROM python:3.12-slim

WORKDIR /app

COPY llm_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Stubs are generated flat into /app (on sys.path), so llm_server/main.py
# imports them by bare name. The service itself stays a package
# (llm_server/) so its relative imports and the unit tests work unchanged.
COPY proto/ proto/
RUN python -m grpc_tools.protoc -I proto --python_out=. --grpc_python_out=. \
    proto/llm.proto

COPY llm_server/ llm_server/

CMD ["python", "-m", "llm_server.main"]
