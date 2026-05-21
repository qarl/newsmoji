#!/usr/bin/env bash
#
# deploy.sh - install / refresh the newsmoji runtime on Einstein.
#
# Run this ON Einstein. It is idempotent: re-run any time after editing
# newsmoji.py. It copies the script from the repo into a stable runtime dir
# (~/newsmoji) separate from the git working tree, ensures the ssh publish
# alias exists, and installs the */5 cron job.
#
#   ./deploy.sh            install runtime + cron, publishing DISABLED
#   PUBLISH=1 ./deploy.sh  same, but enable publishing to the web host
#
# ONE-TIME web-host asset (NOT handled here, NOT in the repo): the page
# renders in monochrome via a self-hosted webfont that must sit next to
# index.html on the qarl.com host:
#   scp NotoEmoji-mono.woff newsmoji-web:/home/qqqqarl/qarl.com/newsmoji/
# Without it the page still works but falls back to colour system emoji.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${HOME}/newsmoji"
PYTHON="/usr/bin/python3"
CRON_TAG="# newsmoji (jimmy:newsmoji)"

echo "newsmoji deploy"
echo "  repo:    ${REPO_DIR}"
echo "  runtime: ${RUNTIME_DIR}"

# --- runtime dir + script ---------------------------------------------------
mkdir -p "${RUNTIME_DIR}"
cp "${REPO_DIR}/newsmoji.py" "${RUNTIME_DIR}/newsmoji.py.tmp"
mv -f "${RUNTIME_DIR}/newsmoji.py.tmp" "${RUNTIME_DIR}/newsmoji.py"
chmod +x "${RUNTIME_DIR}/newsmoji.py"
echo "  -> copied newsmoji.py to runtime dir"

if [ -f "${RUNTIME_DIR}/newsmoji.env" ]; then
    chmod 600 "${RUNTIME_DIR}/newsmoji.env"
    echo "  -> newsmoji.env present (chmod 600)"
else
    echo "  !! newsmoji.env MISSING - create it with:"
    echo "        echo 'ANTHROPIC_API_KEY=sk-ant-...' > ${RUNTIME_DIR}/newsmoji.env"
    echo "        chmod 600 ${RUNTIME_DIR}/newsmoji.env"
fi

# --- ssh publish alias ------------------------------------------------------
mkdir -p "${HOME}/.ssh"
touch "${HOME}/.ssh/config"
chmod 600 "${HOME}/.ssh/config"
if grep -qE '^[[:space:]]*Host[[:space:]].*(^|[[:space:]])newsmoji-web([[:space:]]|$)' "${HOME}/.ssh/config"; then
    echo "  -> ssh alias 'newsmoji-web' already present"
else
    cat >> "${HOME}/.ssh/config" <<'EOF'

Host newsmoji-web
    HostName www.qarl.com
    User qqqqarl
    IdentityFile ~/.ssh/newsmoji_publish
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
    ConnectTimeout 15
EOF
    echo "  -> added ssh alias 'newsmoji-web'"
fi

# --- cron job ---------------------------------------------------------------
PUBLISH_ENV=""
if [ "${PUBLISH:-0}" = "1" ]; then
    PUBLISH_ENV="NEWSMOJI_PUBLISH_ENABLED=1 "
    echo "  -> publishing ENABLED"
else
    echo "  -> publishing disabled (run 'PUBLISH=1 ./deploy.sh' to enable)"
fi

CRON_LINE="*/5 * * * * ${PUBLISH_ENV}${PYTHON} ${RUNTIME_DIR}/newsmoji.py >> ${RUNTIME_DIR}/cron.err 2>&1 ${CRON_TAG}"
# De-dup on our own tag only, so an unrelated cron line is never touched.
{ crontab -l 2>/dev/null | grep -vF "${CRON_TAG}" || true; echo "${CRON_LINE}"; } | crontab -
echo "  -> cron installed:"
echo "     ${CRON_LINE}"

echo "done."
