FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY pyproject.toml ./
COPY frontends/webcomponent/ frontends/webcomponent/
WORKDIR /build/frontends/webcomponent
RUN npm install --package-lock=false && npm run sync-version && npx tsc && npx vite build

FROM python:3.12-slim AS wheel
WORKDIR /build
COPY pyproject.toml MANIFEST.in README.md LICENSE ./
COPY src/ src/
COPY data/catalog/ data/catalog/
COPY examples/embeddable_copilot_shell.html examples/
COPY infra/postgres/*.sql infra/postgres/
COPY --from=frontend /build/frontends/webcomponent/dist/ frontends/webcomponent/dist/
RUN pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.12-slim
RUN useradd --uid 10001 --create-home copilot && mkdir -p /var/lib/data-analysis-agent && chown copilot /var/lib/data-analysis-agent
COPY --from=wheel /wheels /wheels
RUN pip install --no-cache-dir '/wheels/vanna-2.0.2-py3-none-any.whl[product]' && rm -r /wheels
USER copilot
WORKDIR /home/copilot
ENV DATA_ANALYSIS_STATE_DIR=/var/lib/data-analysis-agent DATA_ANALYSIS_DEMO_HOST=0.0.0.0 DATA_ANALYSIS_DEMO_PORT=8000
EXPOSE 8000
CMD ["data-analysis-agent"]
