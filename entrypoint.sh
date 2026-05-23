#!/bin/bash
set -e

if [ -n "${MOODLE_DOCKER_BUNDLED_PATH}" ] && [ ! -f "${MOODLE_DOCKER_BUNDLED_PATH}/base.yml" ]; then
    echo "[moodle-manager] Clonando moodle-docker en ${MOODLE_DOCKER_BUNDLED_PATH}..."
    git clone --depth=1 https://github.com/moodlehq/moodle-docker "${MOODLE_DOCKER_BUNDLED_PATH}"
    echo "[moodle-manager] moodle-docker listo."
fi

exec uvicorn main:app --host 0.0.0.0 --port 9000 --reload
