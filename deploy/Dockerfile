# Single image, reused by 3 services (capture / control-api / training).
# Each one picks a different command + a different BILBO_EXTRAS at build
# time so the heavy torch/cv2 stack is only installed where it's needed.
FROM python:3.12-slim AS base

# Build args
ARG BILBO_EXTRAS=

# System deps:
#   ffmpeg          — RTSP capture (used by bilbo.pipeline.capture)
#   libgl1, libglib2.0-0 — opencv-python-headless runtime deps
# Kept minimal so the image is small when BILBO_EXTRAS=control-api (no ml).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the bits pip needs and install. References ship in the image so the
# bilbo.pipeline.vision prompt loader can find prompt.md inside the
# container without a separate bind mount.
COPY pyproject.toml ./
COPY src/ ./src/
COPY references/ ./references/

# When BILBO_EXTRAS is empty (control-api spawns training with an empty
# extras arg, for instance) install bare bilbo. Otherwise install with the
# requested extra.
RUN if [ -z "$BILBO_EXTRAS" ]; then \
        pip install --no-cache-dir -e .; \
    else \
        pip install --no-cache-dir -e ".[$BILBO_EXTRAS]"; \
    fi

ENV BILBO_ROOT=/app \
    PYTHONUNBUFFERED=1

# Listed for documentation — published only where compose maps them:
#   5555 dashboard, 5556 control-api, 5557 capture (internal)
EXPOSE 5555 5556 5557

# Default command is overridden in docker-compose per service.
CMD ["bilbo-monitor", "--help"]
