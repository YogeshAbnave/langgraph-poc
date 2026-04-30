FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app

# Configure UV for container environment
ENV UV_SYSTEM_PYTHON=1 UV_COMPILE_BYTECODE=1

# Copy and install agent dependencies first (layer caching)
COPY app/MyAgent/pyproject.toml app/MyAgent/pyproject.toml
COPY app/MyAgent/main.py app/MyAgent/main.py

# Install from pyproject.toml directory
RUN cd app/MyAgent && uv pip install .

# Install observability
RUN uv pip install "aws-opentelemetry-distro>=0.10.1"

# Set AWS region environment variable
ENV AWS_REGION=us-east-2
ENV AWS_DEFAULT_REGION=us-east-2

# Signal that this is running in Docker for host binding logic
ENV DOCKER_CONTAINER=1

# Create non-root user and set ownership
RUN useradd -m -u 1000 bedrock_agentcore && chown -R bedrock_agentcore /app
USER bedrock_agentcore

EXPOSE 8080
EXPOSE 8000

CMD ["python", "app/MyAgent/main.py"]
