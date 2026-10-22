# Hugging Face Spaces (Docker SDK). Serves the Streamlit app on port 7860.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

# HF Spaces runs containers as uid 1000.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/app/.venv/bin:$PATH \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /home/user/app

COPY --chown=user pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=user cqa ./cqa
COPY --chown=user app ./app
COPY --chown=user .streamlit ./.streamlit
COPY --chown=user eval ./eval

EXPOSE 7860
CMD ["streamlit", "run", "app/streamlit_app.py", "--server.port=7860", "--server.address=0.0.0.0"]
