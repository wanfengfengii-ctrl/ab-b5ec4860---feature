FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY verify ./verify

EXPOSE 8000

# Default command: run the API.  The compose `verify` service overrides this
# with `python -m verify.run`.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
