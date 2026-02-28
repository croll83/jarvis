FROM node:22-alpine
RUN addgroup -S app && adduser -S app -G app
WORKDIR /app
COPY server.js .
USER app
EXPOSE 8003
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://localhost:8003/health || exit 1
CMD ["node", "server.js"]
