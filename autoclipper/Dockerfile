FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fonts-montserrat fontconfig tini curl \
 && rm -rf /var/lib/apt/lists/* && fc-cache -f

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# YuNet face detector (OpenCV Zoo, MIT licence), pinned by checksum
RUN mkdir -p /opt/models && curl -fsSL -o /opt/models/face_detection_yunet_2023mar.onnx \
      https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx \
 && echo "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4  /opt/models/face_detection_yunet_2023mar.onnx" | sha256sum -c -

COPY autoclipper ./autoclipper

# Run as an unprivileged user; /data is the only writable volume.
RUN useradd -m -u 1000 clipper && mkdir -p /data && chown clipper:clipper /data
USER clipper
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "autoclipper"]
