FROM python:3.11-slim

WORKDIR /app

COPY backend/requirements.txt .

RUN python -m pip install --upgrade pip setuptools wheel

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY . .

WORKDIR /app/backend

EXPOSE 8000

CMD ["python", "run.py"]