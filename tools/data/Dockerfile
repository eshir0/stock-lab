FROM python:3.12-slim
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir pandas==2.* pyarrow duckdb requests numpy
RUN useradd -u 10001 -m collector
WORKDIR /code
USER collector
