#!/usr/bin/env bash
# DEVELOPMENT environment identity. Sourced by dev/deploy.sh and dev/provision.sh
# before deploy/lib.sh.
#
# IDENTITY VALUES ARE HARD-CODED, NOT ENV-OVERRIDABLE — deliberately the
# opposite of the old single config.sh. An `${VAR:-default}` override is exactly
# how a stray exported variable could silently retarget this script at the
# production VM; with fixed values plus the cross-target guard in lib.sh, a dev
# deploy structurally cannot name a prod resource. To change where dev lives,
# edit this file.

DEPLOY_ENV="dev"

# Its OWN resource group — not shared with prod. Everything this environment
# creates (VM, NIC, IP, NSG, disk) lives here, so even `az group delete` on it
# cannot touch production.
RESOURCE_GROUP="elite-dev-rg"
LOCATION="uaenorth"        # same region as prod: keeps the PDPL residency story identical

VM_NAME="elite-dev-vm"
VM_SIZE="Standard_B2ms"    # 2 vCPU / 8 GB — same shape as prod (Vite build headroom);
                           # provision-core falls back through equivalents if unavailable
VM_IMAGE="Ubuntu2204"
ADMIN_USER="azureuser"

# https://elite-dev.uaenorth.cloudapp.azure.com
DNS_LABEL="elite-dev"
ACME_EMAIL="syed.hanif@digycorp.com"

# ALWAYS EMPTY FOR DEV. The custom domain belongs to production alone — a dev VM
# that answers for it burns failed ACME attempts against the prod hostname
# (happened 2026-07-28). lib.sh refuses to run a non-prod deploy with this set.
CUSTOM_DOMAIN=""

# TURN RELAY (2026-08-04) — dev only, for now.
#
# Opens UDP 3478 + TCP 3478 + UDP 49160-49200 at the NSG so the coturn service
# in this environment's compose overlay is reachable. Without it the voice
# call's WebRTC transport can never connect (13/13 ICE failures, measured) and
# every call degrades to WS-PCM with no echo cancellation.
#
# provision-core.sh is shared with prod, so this flag — not the shared file —
# is what keeps production's firewall exactly as it is. Set it in
# deploy/prod/config.sh only as a deliberate decision, and pair it with
# time-limited shared-secret TURN credentials rather than a static user.
TURN_RELAY=1
