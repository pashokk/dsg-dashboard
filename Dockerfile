# Static site (docs/) served by nginx, plus Python so the same container
# can run scripts/fetch_data.py — on start, and again on a schedule via
# Coolify's Scheduled Tasks feature. Set JIRA_BASE_URL / JIRA_EMAIL /
# JIRA_API_TOKEN as this app's Environment Variables in Coolify.
FROM nginx:alpine

RUN apk add --no-cache python3

COPY docs /usr/share/nginx/html
COPY scripts /app/scripts
COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV DATA_OUTPUT_PATH=/usr/share/nginx/html/data.json

EXPOSE 80
ENTRYPOINT ["/entrypoint.sh"]
