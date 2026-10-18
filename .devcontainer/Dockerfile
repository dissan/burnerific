FROM python:3.14-bookworm

# Native bits some Home Assistant core integrations want at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libturbojpeg0 libpcap-dev bluez \
    && rm -rf /var/lib/apt/lists/*

# Home Assistant installs integration requirements on demand, and only does that
# properly inside a virtualenv.
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
RUN python -m venv "$VIRTUAL_ENV"

COPY requirements_dev.txt /tmp/requirements_dev.txt
RUN pip install --no-cache-dir --upgrade pip uv \
    && pip install --no-cache-dir -r /tmp/requirements_dev.txt
