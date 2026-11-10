FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mahalanobis_analyse.py vm_export.py mahalanobis_service.py ./

# /config: config.yml (read-only),  /data: state_file (z. B. state_file: /data/state.json)
RUN mkdir -p /data && chown nobody /data
VOLUME ["/data"]
USER nobody

ENTRYPOINT ["python", "-u", "mahalanobis_service.py"]
CMD ["-c", "/config/config.yml"]
