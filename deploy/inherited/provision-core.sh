#!/usr/bin/env bash
# One-time Azure infrastructure for ONE environment (AD-G).
#
# NOT AN ENTRY POINT. Run the per-environment wrappers, which load the right
# config first and then source this:
#
#   ./deploy/dev/provision.sh
#   ./deploy/prod/provision.sh
#
# You do NOT normally run even those by hand — each environment's deploy.sh
# calls its provision.sh automatically when the VM is missing. It is a separate
# file because "create infrastructure" and "ship code" have very different blast
# radii, and mixing them makes the second one scary to re-run.
#
# IDEMPOTENT: every step checks before it creates, so a re-run against existing
# infrastructure is a no-op rather than an error or a duplicate.

set -euo pipefail

# say/ok/warn/die and every *_ID / derived value come from lib.sh, which the
# wrapper sourced after its config. If they are missing, someone ran this file
# directly — refuse rather than provision an unnamed target.
if ! declare -F die >/dev/null || [[ -z "${DEPLOY_ENV:-}" ]]; then
  printf 'provision-core.sh is not an entry point. Run:\n' >&2
  printf '    ./deploy/dev/provision.sh   or   ./deploy/prod/provision.sh\n' >&2
  exit 2
fi

# --- Preflight --------------------------------------------------------------
command -v az >/dev/null || die "Azure CLI not found. brew install azure-cli"

az account show >/dev/null 2>&1 || die "Not logged in to Azure. Run: az login"

SUB_NAME="$(az account show --query name -o tsv)"
SUB_ID="$(az account show --query id -o tsv)"

say "Provisioning [${DEPLOY_ENV}] into subscription: ${SUB_NAME} (${SUB_ID})"
echo "    environment    : ${DEPLOY_ENV}"
echo "    resource group : ${RESOURCE_GROUP}"
echo "    location       : ${LOCATION}"
echo "    vm             : ${VM_NAME} (${VM_SIZE})"
echo "    public name    : ${FQDN}"

# --- Resource providers -----------------------------------------------------
# A fresh subscription has these unregistered, and `az vm create` then fails
# partway through with an error that does not name the real cause. Registration
# is free, creates nothing, and is a no-op once done — so it is cheaper to check
# every run than to debug it once.
say "Checking resource provider registration"
for rp in Microsoft.Compute Microsoft.Network; do
  state="$(az provider show -n "$rp" --query registrationState -o tsv 2>/dev/null || echo Unknown)"
  if [[ "$state" == "Registered" ]]; then
    ok "${rp} registered"
  else
    echo "    ${rp} is ${state} — registering (one time, a few minutes)"
    az provider register -n "$rp" >/dev/null 2>&1 || true
    for i in $(seq 1 60); do
      state="$(az provider show -n "$rp" --query registrationState -o tsv 2>/dev/null || echo Unknown)"
      [[ "$state" == "Registered" ]] && { ok "${rp} registered"; break; }
      [[ $i -eq 60 ]] && die "${rp} did not reach Registered (stuck at ${state}).
  Check the subscription has permission to register providers:
      az provider show -n ${rp}"
      sleep 10
    done
  fi
done

# --- Resource group ---------------------------------------------------------
if az group show -n "$RESOURCE_GROUP" >/dev/null 2>&1; then
  ok "resource group ${RESOURCE_GROUP} already exists"
else
  say "Creating resource group ${RESOURCE_GROUP}"
  az group create -n "$RESOURCE_GROUP" -l "$LOCATION" -o none
  ok "created"
fi

# --- The VM -----------------------------------------------------------------
if az vm show -g "$RESOURCE_GROUP" -n "$VM_NAME" >/dev/null 2>&1; then
  ok "vm ${VM_NAME} already exists — skipping create"
else
  say "Creating VM ${VM_NAME} (this takes a few minutes)"

  # cloud-init installs Docker at first boot. Generated here rather than kept as
  # a static file so it can interpolate ADMIN_USER and REMOTE_DIR from config —
  # a second file with a hardcoded username is exactly the kind of drift the
  # single-config rule exists to prevent.
  CLOUD_INIT="$(mktemp -t elite-cloud-init.XXXXXX)"
  trap 'rm -f "$CLOUD_INIT"' EXIT

  cat >"$CLOUD_INIT" <<CLOUDINIT
#cloud-config
package_update: true
packages:
  - ca-certificates
  - curl
  - gnupg
  - rsync
runcmd:
  - install -m 0755 -d /etc/apt/keyrings
  - curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  - chmod a+r /etc/apt/keyrings/docker.asc
  - echo "deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \$(. /etc/os-release && echo \$VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
  - apt-get update
  - apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  - usermod -aG docker ${ADMIN_USER}
  - mkdir -p ${REMOTE_DIR}
  - chown -R ${ADMIN_USER}:${ADMIN_USER} ${REMOTE_DIR}
  # Written LAST, purely as a human breadcrumb that cloud-init ran to completion.
  # NOTHING DEPENDS ON IT: deploy-core.sh probes 'docker info' instead, because
  # this (quotes, NOT backticks: this heredoc is unquoted so it can interpolate
  # ADMIN_USER/REMOTE_DIR, which means backticks EXECUTE on the deploy machine
  # and splice their output into the YAML -- that corrupted cloud-init once,
  # and the only symptom was a VM with no Docker and a 24-second cloud-init)
  # path is inside REMOTE_DIR and the deploy rsync (--delete) removes it every
  # time. It is left here only because it is genuinely useful when SSHing in to
  # ask "did cloud-init finish, or is it still going?".
  #
  # KEEP THIS FILE PURE ASCII. The Azure CLI encodes --custom-data as latin-1,
  # so a single em dash or smart quote in a comment fails the whole vm create
  # with an opaque codec error and no VM. Enforced by a guard below.
  - touch ${REMOTE_DIR}/.provisioned
CLOUDINIT

  # Guard: the Azure CLI encodes --custom-data as latin-1, and any character
  # outside it fails `vm create` with a bare codec error that names a byte
  # offset rather than the problem. Catch it here, where we can say what to fix.
  # `tr -d '\000-\177'` deletes every ASCII byte; whatever remains is non-ASCII.
  # This is byte-oriented and portable, unlike the two obvious alternatives:
  # `grep -P` is GNU-only (absent on macOS), and `grep '[^[:print:]]'` silently
  # matches nothing on BSD grep even under LC_ALL=C. Both fail OPEN, which is
  # the worst outcome for a guard -- it would look present and do nothing.
  NON_ASCII_COUNT="$(LC_ALL=C tr -d '\000-\177' < "$CLOUD_INIT" | wc -c | tr -d ' ')"
  if [[ "$NON_ASCII_COUNT" -gt 0 ]]; then
    printf '\nNon-ASCII bytes found: %s\n' "$NON_ASCII_COUNT" >&2
    die "cloud-init contains non-ASCII characters (em dash? smart quotes?).
  The Azure CLI encodes --custom-data as latin-1 and fails on them with an
  opaque codec error naming only a byte offset.
  Replace them with plain ASCII in the heredoc above."
  fi

  # SIZE FALLBACK. Smaller regions (uaenorth among them) routinely refuse a
  # given SKU with "Capacity Restrictions" -- a transient, region-wide condition
  # that has nothing to do with quota and does NOT reliably show up in
  # `az vm list-skus` beforehand, because that reports SUBSCRIPTION restrictions
  # rather than live capacity. The only dependable test is to attempt a create.
  #
  # So: try candidates in order and take the first that works. Every entry is
  # 2 vCPU / 8 GB, so whichever wins matches the sizing decision (build headroom
  # for the Vite/TypeScript step); they differ in price and processor, not shape.
  # Ordered cheapest-first. (Prod itself landed on Standard_D2as_v4 this way.)
  VM_SIZE_CANDIDATES=(
    "$VM_SIZE"
    Standard_B2s_v2
    Standard_D2as_v5
    Standard_D2s_v5
    Standard_D2as_v4
    Standard_D2s_v3
  )

  vm_created=0
  for size in "${VM_SIZE_CANDIDATES[@]}"; do
    echo "    trying ${size}..."
    if create_out="$(az vm create \
        --resource-group "$RESOURCE_GROUP" \
        --name "$VM_NAME" \
        --image "$VM_IMAGE" \
        --size "$size" \
        --admin-username "$ADMIN_USER" \
        --generate-ssh-keys \
        --public-ip-address-dns-name "$DNS_LABEL" \
        --public-ip-sku Standard \
        --os-disk-size-gb 64 \
        --custom-data "$CLOUD_INIT" \
        -o none 2>&1)"; then
      ok "vm created with ${size}"
      VM_SIZE="$size"
      vm_created=1
      break
    fi

    # Capacity/availability -> try the next candidate. ANYTHING ELSE (bad
    # credentials, a malformed argument, policy denial) must stop immediately:
    # walking the whole list on an unrelated error would bury the real message
    # under five identical failures.
    #
    # InvalidTemplateDeployment / "preflight validation" are in this list
    # because of an Azure CLI bug (seen on 2.86.0): when a SKU is unavailable,
    # the CLI CRASHES while formatting the error -- 'NoneType' object has no
    # attribute 'error', then "content for this response was already consumed"
    # -- and the inner SkuNotAvailable detail never prints. Matching only the
    # inner message therefore works for some attempts and not others, which is
    # exactly the flakiness that cost a deploy cycle here. The outer wrapper is
    # the only text guaranteed to survive the crash.
    #
    # Cost of over-matching: a genuinely malformed template would retry five
    # times before failing. Acceptable -- the final die() lists every size tried,
    # so the pattern is visible rather than mysterious.
    if grep -qiE 'SkuNotAvailable|Capacity Restrictions|not available in location|AllocationFailed|InvalidTemplateDeployment|preflight validation' <<<"$create_out"; then
      warn "${size} unavailable in ${LOCATION} right now -- trying next candidate"
      continue
    fi

    printf '\n%s\n' "$create_out" >&2
    die "vm create failed for a reason unrelated to capacity (see above)."
  done

  if [[ $vm_created -ne 1 ]]; then
    die "Every candidate size was capacity-restricted in ${LOCATION}.
  Options, in the order worth trying:
    1. Wait -- capacity restrictions are usually transient (minutes to hours).
    2. Edit VM_SIZE in deploy/${DEPLOY_ENV}/config.sh to a size that is available.
    3. Try again later in the day -- uaenorth capacity fluctuates."
  fi
fi

# --- DNS label --------------------------------------------------------------
# Set explicitly as well as at create time: an existing VM from an earlier run
# may predate the label, and the certificate depends on this name resolving.
PUBLIC_IP_NAME="$(az vm list-ip-addresses -g "$RESOURCE_GROUP" -n "$VM_NAME" \
  --query "[0].virtualMachine.network.publicIpAddresses[0].name" -o tsv)"

CURRENT_LABEL="$(az network public-ip show -g "$RESOURCE_GROUP" -n "$PUBLIC_IP_NAME" \
  --query "dnsSettings.domainNameLabel" -o tsv 2>/dev/null || true)"

if [[ "$CURRENT_LABEL" == "$DNS_LABEL" ]]; then
  ok "dns label already set to ${DNS_LABEL}"
else
  say "Setting DNS label ${DNS_LABEL}"
  az network public-ip update -g "$RESOURCE_GROUP" -n "$PUBLIC_IP_NAME" \
    --dns-name "$DNS_LABEL" -o none
  ok "set"
fi

# --- Network security -------------------------------------------------------
# 22 (deploy), 80 (ACME challenge + redirect), 443 (the site). NOTHING ELSE.
# 8000/8080 are published on the VM's host interface by the base compose file
# and are deliberately NOT opened here — see the note at the foot of the
# per-environment docker-compose.yml overlay.
say "Configuring network security rules"
open_port() {
  local name="$1" port="$2" priority="$3" proto="${4:-Tcp}"
  if az network nsg rule show -g "$RESOURCE_GROUP" --nsg-name "${VM_NAME}NSG" -n "$name" >/dev/null 2>&1; then
    ok "rule ${name} (${proto} ${port}) already present"
  else
    az network nsg rule create \
      -g "$RESOURCE_GROUP" --nsg-name "${VM_NAME}NSG" \
      -n "$name" --priority "$priority" \
      --destination-port-ranges "$port" --access Allow --protocol "$proto" \
      -o none
    ok "opened ${proto} port ${port} (${name})"
  fi
}
open_port "allow-http"  80  1001
open_port "allow-https" 443 1002

# --- TURN relay (2026-08-04) — OPT-IN PER ENVIRONMENT ------------------------
# The voice call's WebRTC transport (AD-M) cannot connect off localhost without
# a relay: the backend offers only its Docker-bridge address behind two NATs and
# the browser offers only its LAN address, so ICE has nothing to pair (measured:
# 13/13 failures on the dev VM). coturn fixes that, and needs UDP — which this
# NSG has never admitted.
#
# ⚠️ GATED ON PURPOSE. This file is SHARED by dev and prod, so an ungated rule
# here would open UDP on production at its next provision as a side effect of a
# dev experiment. Only deploy/dev/config.sh sets TURN_RELAY=1 today. When prod
# earns this, set it there deliberately — and give prod time-limited
# shared-secret credentials rather than the dev VM's static user.
if [[ "${TURN_RELAY:-0}" == "1" ]]; then
  say "Opening TURN relay ports (this environment sets TURN_RELAY=1)"
  open_port "allow-turn-udp"   3478         1003 Udp
  open_port "allow-turn-tcp"   3478         1004 Tcp
  # The relay range, kept deliberately narrow — one call uses 1-2 ports. It
  # MUST match --min-port/--max-port in the environment's compose overlay; a
  # mismatch fails silently, as a call that negotiates and then hears nothing.
  open_port "allow-turn-relay" 49160-49200  1005 Udp
else
  ok "TURN relay ports not opened (TURN_RELAY unset for ${DEPLOY_ENV})"
fi

PUBLIC_IP="$(az vm list-ip-addresses -g "$RESOURCE_GROUP" -n "$VM_NAME" \
  --query "[0].virtualMachine.network.publicIpAddresses[0].ipAddress" -o tsv)"

say "Infrastructure ready [${DEPLOY_ENV}]"
echo "    public ip : ${PUBLIC_IP}"
echo "    fqdn      : ${FQDN}"
echo "    ssh       : ssh ${ADMIN_USER}@${FQDN}"
