FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HF_HOME=/opt/hf PORT=8000
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fontconfig curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-deploy.txt .
RUN pip install -r requirements-deploy.txt
# bake the faster-whisper base model (CPU, float32 as used by cutter_v2)
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='float32')"
COPY . .
# runs/ is a Fly volume (empty on first boot) -> keep the preload demo run outside it and seed on start
RUN mkdir -p /app/preload && for d in runs/v2_niosh_multi runs/demo_pd_kendra runs/demo_pd_calvin; do \
      if [ -d "$d" ]; then mv "$d" /app/preload/; fi; \
    done
RUN chmod +x docker-entrypoint.sh
EXPOSE 8000
CMD ["./docker-entrypoint.sh"]
