# Build the Next.js app without shipping local state or model credentials.
FROM node:22-bookworm-slim AS web
WORKDIR /source
COPY package.json package-lock.json tsconfig.base.json ./
COPY packages/contracts ./packages/contracts
COPY apps/insightpilot ./apps/insightpilot
RUN npm ci --no-audit --no-fund
ENV NEXT_TELEMETRY_DISABLED=1 INSIGHTPILOT_STANDALONE=1
RUN npm run build

FROM python:3.11-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY references/askdata_studio/backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && useradd --create-home --uid 10001 app
COPY --from=web /usr/local/bin/node /usr/local/bin/node
COPY --from=web /source/apps/insightpilot/.next/standalone ./
COPY --from=web /source/apps/insightpilot/.next/static ./apps/insightpilot/.next/static
COPY --from=web /source/apps/insightpilot/public ./apps/insightpilot/public
COPY references/askdata_studio/backend/app ./references/askdata_studio/backend/app
COPY data/homenova_phase_d/public ./data/homenova_phase_d/public
COPY data/homenova_phase_e/public_semantics ./data/homenova_phase_e/public_semantics
COPY deploy/start.py ./deploy/start.py
RUN mkdir -p references/askdata_studio/backend/data && chown -R app:app /app
USER app
ENV PORT=10000 API_PROXY_TARGET=http://127.0.0.1:8001
EXPOSE 10000
CMD ["python", "deploy/start.py"]
