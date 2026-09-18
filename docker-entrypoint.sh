#!/bin/sh
set -e

# Best-effort: pull fresh Jira data on container start. If JIRA_API_TOKEN
# isn't set (or Jira is unreachable), skip it and keep serving whatever's
# already there (the sample data baked into the image, or a previous run's
# data.json) rather than failing the whole container.
if [ -n "$JIRA_API_TOKEN" ]; then
  echo "Fetching DSG data from Jira..."
  python3 /app/scripts/fetch_data.py || echo "fetch_data.py failed, serving existing data.json"
else
  echo "JIRA_API_TOKEN not set, skipping fetch (serving existing/sample data)."
fi

exec nginx -g "daemon off;"
