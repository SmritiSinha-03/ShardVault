# MinIO server image, built from the open-source conda-forge package.
#
# The official minio/minio image is no longer publicly available on Docker
# Hub, so this installs the same AGPLv3 MinIO server from conda-forge
# (https://anaconda.org/conda-forge/minio-server). The binary is statically
# linked, so the final image is just Debian slim plus one file.

FROM debian:bookworm-slim AS fetch

ARG MINIO_VERSION=2025.01.20.14.49.07

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl bzip2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp
RUN set -eux; \
    case "$(uname -m)" in \
        x86_64) platform=linux-64 ;; \
        aarch64 | arm64) platform=linux-aarch64 ;; \
        *) echo "Unsupported CPU architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/mamba-org/micromamba-releases/releases/latest/download/micromamba-${platform}.tar.bz2" \
        | tar -xjf - bin/micromamba; \
    MAMBA_ROOT_PREFIX=/tmp/mamba ./bin/micromamba create --yes --prefix /opt/minio \
        --channel conda-forge --override-channels "minio-server=${MINIO_VERSION}"; \
    /opt/minio/bin/minio --version

FROM debian:bookworm-slim
COPY --from=fetch /opt/minio/bin/minio /usr/local/bin/minio
EXPOSE 9000 9001
ENTRYPOINT ["minio"]
CMD ["server", "/data"]
