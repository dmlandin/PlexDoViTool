FROM python:3.11-slim

# ffmpeg -> ffprobe/ffmpeg; mkvtoolnix -> mkvmerge/mkvextract/mkvpropedit (used by stage 2).
# curl + ca-certificates are only needed to fetch the pinned dovi_tool release.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      mkvtoolnix \
      curl \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# dovi_tool — pinned to a specific release for reproducible builds (NOT "latest").
# https://github.com/quietvoid/dovi_tool/releases/tag/2.3.2
ARG DOVI_TOOL_VERSION=2.3.2
RUN curl -fsSL \
      "https://github.com/quietvoid/dovi_tool/releases/download/${DOVI_TOOL_VERSION}/dovi_tool-${DOVI_TOOL_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
      -o /tmp/dovi_tool.tar.gz \
 && tar -xzf /tmp/dovi_tool.tar.gz -C /usr/local/bin dovi_tool \
 && chmod +x /usr/local/bin/dovi_tool \
 && rm /tmp/dovi_tool.tar.gz \
 && dovi_tool --version

RUN pip install --no-cache-dir pyyaml==6.0.2

WORKDIR /app
COPY audit.py /app/audit.py
COPY config.yaml /app/config.yaml

ENTRYPOINT ["python", "/app/audit.py"]
CMD ["--config", "/app/config.yaml"]
