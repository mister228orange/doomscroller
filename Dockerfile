FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install the package first so dependency layers cache independently of source edits.
COPY pyproject.toml README.md ./
COPY doomscroller ./doomscroller
RUN pip install --no-cache-dir .

# The repo is bind-mounted at /app by compose (so .env and the Telethon session are
# reused as-is); the installed package lives in site-packages and is unaffected.
CMD ["python", "-m", "doomscroller", "poll", "--loop"]
