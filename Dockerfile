FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
COPY providers ./providers
COPY scripts ./scripts
RUN pip install --no-cache-dir '.[dev]' && useradd --create-home runner
COPY . .
RUN chown -R runner:runner /srv
USER runner
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
