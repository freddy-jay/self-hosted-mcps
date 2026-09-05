ARG BASE_IMAGE=localhost/mcps-audit-runtime:20260905
FROM ${BASE_IMAGE}
COPY tests /tests
CMD ["node", "/tests/container-smoke.js"]
