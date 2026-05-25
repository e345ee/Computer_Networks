FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JOB_PORT=7001

COPY app ./app
EXPOSE 7001
CMD ["python", "-m", "app.server"]
