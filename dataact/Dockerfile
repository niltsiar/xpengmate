# The export request service: logs in to the XPENG Data Act portal with a headless Chromium.
FROM node:22.23.5-bookworm-slim@sha256:66cb8ef75184df13dc0dcf866330488996b485d404db60555756865300ced4a5

# The browser must sit outside /root so user node can start it.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund \
 && npx playwright-core install --with-deps --only-shell chromium \
 && rm -rf /root/.npm /var/lib/apt/lists/* \
 && mkdir /data && chown node:node /data

COPY *.mjs ./

USER node
EXPOSE 8300

CMD ["node", "server.mjs"]
