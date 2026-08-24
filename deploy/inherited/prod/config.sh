#!/usr/bin/env bash
# PRODUCTION environment identity. Sourced by prod/deploy.sh and prod/provision.sh
# before deploy/lib.sh.
#
# IDENTITY VALUES ARE HARD-CODED, NOT ENV-OVERRIDABLE — deliberately the
# opposite of the old single config.sh. An `${VAR:-default}` override is exactly
# how CUSTOM_DOMAIN="" once got silently upgraded back to the prod domain and
# a dev deploy ended up answering for production. Environments are now separate
# directories, so there is nothing left to select at run time: this file IS
# production. To change where prod lives, edit this file — and mirror the
# identity constants in deploy/lib.sh's cross-target guard.

DEPLOY_ENV="prod"

RESOURCE_GROUP="elite-demo-rg"
LOCATION="uaenorth"        # HLD deploy target; keeps business data in-region for PDPL

VM_NAME="elite-demo-vm"
VM_SIZE="Standard_B2ms"    # only used if the VM ever has to be recreated; the
                           # existing prod VM actually runs Standard_D2as_v4
                           # (capacity fallback at creation time)
VM_IMAGE="Ubuntu2204"
ADMIN_USER="azureuser"

# https://elite-demo.uaenorth.cloudapp.azure.com — the demo-day QR target's
# fallback name. Azure attaches the label to the public IP at no cost; Caddy
# then obtains a Let's Encrypt certificate for it automatically.
#
# THIS IS WHAT MAKES THE MIC WORK. getUserMedia is blocked on plain HTTP
# everywhere except localhost, so without a genuine certificate the mic button
# is dead on every attendee's phone and no code change fixes it.
DNS_LABEL="elite-demo"
ACME_EMAIL="syed.hanif@digycorp.com"

# A vanity hostname served ALONGSIDE the Azure FQDN, its A record pointed at the
# VM's public IP. Caddy obtains a certificate for each name it is given, and the
# Azure FQDN stays in the served list as the fallback that still works when the
# custom DNS record is wrong or mid-propagation.
CUSTOM_DOMAIN="elite-group.digycorp.com"
