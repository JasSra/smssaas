FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    android-tools-adb \
    curl \
    python3 \
    && rm -rf /var/lib/apt/lists/*

COPY watcher.sh /watcher.sh
COPY sms_worker.py /sms_worker.py
RUN chmod +x /watcher.sh

CMD ["/watcher.sh"]
