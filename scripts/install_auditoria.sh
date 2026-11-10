#!/usr/bin/env bash
# Install APS-Conecta/auditoria on the test topology, for gestion-aio#106.
#
# tests/integration/test_audit_log_live.py reads each write the connector records back from
# admin_audit's audit.log. This script builds the state it needs, the way auditoria's own
# `make instance-up` does: admin_audit and auditoria enabled, admin_audit's info lines let
# through the default log level, and a restart, because the web workers cache the routes
# and the enabled apps in APCu, which occ cannot clear. auditoria has no App release yet,
# so its source comes from a pinned commit.
#
# Environment, all optional:
#   AUDITORIA_REF   commit of APS-Conecta/auditoria, default the merge of auditoria#1
#   NC_CONTAINER    the Nextcloud container, default nc-mcp-exapp-nc
set -euo pipefail

AUDITORIA_REF="${AUDITORIA_REF:-12fbd0c549836bb945e65395734b64cef1e09265}"
NC_CONTAINER="${NC_CONTAINER:-nc-mcp-exapp-nc}"

# The same MSYS guard as scripts/install_findling.sh: docker exec paths stay unconverted.
dockerx() {
  MSYS_NO_PATHCONV=1 docker "$@"
}

occ() {
  dockerx exec --user www-data "${NC_CONTAINER}" php occ "$@"
}

workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT

echo "fetching auditoria at ${AUDITORIA_REF}"
curl -sfL "https://github.com/APS-Conecta/auditoria/archive/${AUDITORIA_REF}.tar.gz" |
  tar -xz -C "${workdir}"
mkdir "${workdir}/auditoria"
cp -r "${workdir}/auditoria-${AUDITORIA_REF}/appinfo" "${workdir}/auditoria-${AUDITORIA_REF}/lib" \
  "${workdir}/auditoria/"

echo "installing it into custom_apps"
docker cp "${workdir}/auditoria" "${NC_CONTAINER}:/var/www/html/custom_apps/auditoria"
dockerx exec --user root "${NC_CONTAINER}" chown -R www-data:www-data \
  /var/www/html/custom_apps/auditoria
occ app:enable admin_audit auditoria
occ config:system:set log.condition apps 0 --value=admin_audit

echo "restarting ${NC_CONTAINER}"
docker restart "${NC_CONTAINER}" >/dev/null
for _ in $(seq 60); do
  if [ "$(docker inspect -f '{{.State.Health.Status}}' "${NC_CONTAINER}")" = healthy ]; then
    echo "auditoria is installed"
    exit 0
  fi
  sleep 2
done
echo "ERROR: ${NC_CONTAINER} did not become healthy after the restart" >&2
exit 1
