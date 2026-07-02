FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    cifs-utils \
    ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Install a specific, stable version of uv
RUN curl -LsSf https://astral.sh/uv/0.5.0/install.sh | env UV_UNMANAGED_INSTALL="/usr/local/bin" sh

# Set up working directory
WORKDIR /deps/deep_research

# Copy the local package
ADD . /deps/deep_research
RUN cp /deps/deep_research/.env.docker /deps/deep_research/.env

# Note: Runtime directories (docs/, output/, input/) are mounted via Azure Files at runtime
# No need to create them in the Docker image - they will be provided by the volume mount

# Use pip directly instead of uv sync to avoid segfault
RUN pip install --no-cache-dir -e .

# Set the host for the dev server
ENV HOST=0.0.0.0
ENV PORT=2024

EXPOSE 2024

# Copy and set up entrypoint script
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Use entrypoint to setup symlinks
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# Launch using langgraph dev
CMD ["langgraph", "dev", "--host", "0.0.0.0", "--port", "2024", "--no-reload", "--no-browser"]

