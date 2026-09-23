# The archive runner: Python + Playwright's Chromium + pikepdf, nothing else.
# The script itself is not baked in — run.sh mounts this checkout at /app, so
# an edit takes effect on the next run without a rebuild. Built once by
# run.sh (or `container build --tag cam-webarchive --file Dockerfile .`);
# the same image is what a cloud or home-server runner would use.
FROM python:3.12-slim-bookworm

# Fonts: the sites ask for Avenir Next (ArcGIS Experience Builder), Helvetica
# Neue, Arial, Segoe UI — none of which a Linux image has. Without stand-ins
# Chromium falls to DejaVu Sans, which is much wider, so headings wrap onto
# extra lines and overlap fixed-height blocks. Lato and Liberation Sans are
# close in width; container/fonts.conf maps the names to them.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata fonts-lato fonts-liberation fonts-noto-core \
 && pip install --no-cache-dir playwright pikepdf boto3 pyyaml \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*
COPY container/fonts.conf /etc/fonts/local.conf
RUN fc-cache -f

# Mount points run.sh fills: the vault's Web Archive folder and the calendars
# checkout (sources.yaml in, docs/captures.json out). Capture stamps are local
# time, so the container keeps Austin's clock.
ENV CAM_ARCHIVE_ROOT=/archive \
    CAM_CALENDARS_REPO=/calendars \
    TZ=America/Chicago \
    PYTHONUNBUFFERED=1

WORKDIR /app
ENTRYPOINT ["python", "/app/archive_page.py"]
