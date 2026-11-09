# Multi-stage build: the binary is compiled from the sources in this repository (no prebuilt files),
# and the runtime image contains only the binary and an empty /data.
FROM ubuntu:24.04 AS builder

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential cmake ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY CMakeLists.txt ./
COPY src ./src
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
 && cmake --build build -j"$(nproc)" \
 && strip build/minivectordb

FROM ubuntu:24.04

RUN mkdir -p /data
COPY --from=builder /src/build/minivectordb /app/minivectordb
VOLUME ["/data"]

# Foreground process, SQL on stdin, results on stdout, logs on stderr. No network listener.
ENTRYPOINT ["/app/minivectordb"]
CMD ["--data-dir", "/data"]
