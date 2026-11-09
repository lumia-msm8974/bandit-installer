# Test environment with python3-hivex (the BCD writer used on Linux) and the disk
# tools the tests need. Build and run with tests/docker/run-tests.sh.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-hivex mtools fdisk dosfstools parted gzip zstd ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
