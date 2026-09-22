# strata-lang — reproducible runner
# Build: docker build -t ghcr.io/tzinny-dev/strata:0.1.0 .
# Run:   docker run --rm ghcr.io/tzinny-dev/strata:0.1.0 strata --help
#        docker run --rm -v $PWD:/work -w /work ghcr.io/tzinny-dev/strata:0.1.0 strata build examples/daily_orders.strata
FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/Tzinny-dev/strata"
LABEL org.opencontainers.image.description="Strata — declarative, versioned data transformations (strata-lang)"
LABEL org.opencontainers.image.licenses="MIT"

# postgres client libs for psycopg2, no cache
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install from PyPI (reproducible, no local wheel needed).
# strata-lang pulls duckdb+PyYAML; [postgres] adds psycopg2-binary, [all] adds bq/sf if needed.
# Use --no-cache-dir to keep image small.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir "strata-lang[postgres]==0.1.0"

# Copy examples for smoke test / demo (not required at runtime)
COPY examples/ ./examples/
COPY README.md ./

# Default entry
ENTRYPOINT ["strata"]
CMD ["--help"]

# Smoke test at build time
RUN strata --help \
    && strata build examples/daily_orders.strata \
    && strata compile examples/daily_orders.strata --dialect postgres > /tmp/out.sql \
    && test -s /tmp/out.sql
