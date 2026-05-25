FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY airgradient_logger.py .

ENV DB_PATH=/data/airgradient.db
VOLUME ["/data"]

# -u keeps logs unbuffered so docker logs / journald see them immediately.
CMD ["python", "-u", "airgradient_logger.py"]
