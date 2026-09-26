# The analysis sandbox image: Python 3.11 with the analysis libraries, running the
# stdlib-only kernel that `run_python` drives over stdin/stdout.
#
#   docker build -t argo-sandbox argo_bench/sandbox
#
# Nothing is mounted: the console, the run_sql results and the Mission Control journal
# travel in the kernel protocol (argo_bench/servers/python.py, SyncedKernel).
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY kernel.py /opt/sandbox/kernel.py

RUN useradd --create-home --uid 1000 sandbox && mkdir /work && chown sandbox /work
USER 1000
ENV MPLBACKEND=Agg \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/work \
    HOME=/work
WORKDIR /work
ENTRYPOINT ["python", "/opt/sandbox/kernel.py"]
