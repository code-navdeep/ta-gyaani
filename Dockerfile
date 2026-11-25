# TA Gyaani
# One container, one process: the price engine, the metrics loop, the
# end-of-day scheduler, the watchdog and the dashboard (all started by main.py)

# Pinned for reproducible builds. Bump this patch version deliberately.
FROM python:3.13.15-slim

# Force IST timezone for all datetime operations
ENV TZ=Asia/Kolkata
RUN ln -snf /usr/share/zoneinfo/Asia/Kolkata /etc/localtime && echo "Asia/Kolkata" > /etc/timezone

# Set working directory
WORKDIR /app

# Copy requirements first for better caching. The LOCK, not requirements.txt:
# it pins every sub-package to the versions the tests pass against.
COPY requirements.lock .

# Install Python dependencies
RUN pip install --retries 5 --timeout 200 -r requirements.lock

# Copy application code
COPY main.py .
COPY engine.py .
COPY metrics.py .
COPY dashboard.py .
COPY dashboard_signals.py .
COPY config.json .
COPY calculators/ ./calculators/
COPY scripts/ ./scripts/
COPY assets/ ./assets/

# Create data directories
RUN mkdir -p data/logs

# Expose dashboard port
EXPOSE 8054

# Single entry point - main.py starts every part listed at the top
CMD ["python", "main.py"]
