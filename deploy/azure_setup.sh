#!/usr/bin/env bash
# Creates (idempotently) everything Montaj Bot needs on Azure: resource group, Storage Account with the three
# private containers + a 2-day retention rule, a backup container, and ONE VM with ports 22/80/443 open.
#
#   STORAGE_ACCOUNT=montajprod123 ./deploy/azure_setup.sh
#
# Safe to re-run: every resource is checked before it is created. Nothing secret is written to disk; the storage
# connection string is only PRINTED at the end (copy it into .env on the VM, never commit it).
#
# ASSUMPTIONS / # VERIFY: written against Azure CLI 2.x. Flag names below were checked from memory of the CLI
# reference, NOT against a live subscription: run `az <command> --help` if one is rejected. The VM size, region and
# disk are defaults for a SMALL quota -- adjust VM_SIZE/LOCATION to what your subscription allows
# (`az vm list-skus -l <region> --size Standard_B --output table`).
set -euo pipefail

: "${RESOURCE_GROUP:=montaj-rg}"
: "${LOCATION:=westeurope}"                 # pick the region closest to your users that your quota allows
: "${STORAGE_ACCOUNT:?set STORAGE_ACCOUNT (3-24 lowercase letters/digits, globally unique)}"
: "${VM_NAME:=montaj-vm}"
: "${VM_SIZE:=Standard_B2s}"                # 2 vCPU / 4 GB. Renders are CPU-bound: if the quota allows, Standard_B4ms is faster
: "${VM_ADMIN:=azureuser}"
: "${VM_DISK_GB:=64}"                       # renders and downloads use temporary disk: do not go much lower
: "${AZURE_BACKUP_CONTAINER:=backups}"
HERE="$(cd "$(dirname "$0")" && pwd)"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v az >/dev/null || die "Azure CLI (az) is not installed: https://learn.microsoft.com/cli/azure/install-azure-cli"
az account show >/dev/null 2>&1 || die "not logged in: run 'az login' first (and 'az account set --subscription <id>' if you have several)"
[[ "$STORAGE_ACCOUNT" =~ ^[a-z0-9]{3,24}$ ]] || die "STORAGE_ACCOUNT must be 3-24 lowercase letters/digits"
[[ -f "$HERE/lifecycle.json" ]] || die "deploy/lifecycle.json is missing"

log "Resource group $RESOURCE_GROUP ($LOCATION)"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none   # idempotent

log "Storage account $STORAGE_ACCOUNT (Standard_LRS, no public blob access)"
if ! az storage account show --name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" >/dev/null 2>&1; then
  az storage account create \
    --name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" --location "$LOCATION" \
    --sku Standard_LRS --kind StorageV2 --access-tier Hot \
    --allow-blob-public-access false --min-tls-version TLS1_2 --https-only true \
    --output none
fi

CONNECTION_STRING="$(az storage account show-connection-string \
  --name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" --query connectionString --output tsv)"

log "Containers (private): uploads artifacts outputs $AZURE_BACKUP_CONTAINER"
for container in uploads artifacts outputs "$AZURE_BACKUP_CONTAINER"; do
  az storage container create --name "$container" --connection-string "$CONNECTION_STRING" \
    --public-access off --output none                                   # idempotent
done

log "Lifecycle rule: delete blobs in uploads/artifacts/outputs older than 2 days"
az storage account management-policy create \
  --account-name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" \
  --policy "@$HERE/lifecycle.json" --output none                          # replaces the policy: idempotent

log "Virtual machine $VM_NAME ($VM_SIZE)"
if ! az vm show --resource-group "$RESOURCE_GROUP" --name "$VM_NAME" >/dev/null 2>&1; then
  az vm create \
    --resource-group "$RESOURCE_GROUP" --name "$VM_NAME" \
    --image Ubuntu2204 --size "$VM_SIZE" --os-disk-size-gb "$VM_DISK_GB" \
    --admin-username "$VM_ADMIN" --generate-ssh-keys \
    --public-ip-sku Standard --output none                                # creates the NSG with SSH (22) open
  log "Installing Docker, git and the Azure CLI on the VM (takes a few minutes)"
  az vm run-command invoke --resource-group "$RESOURCE_GROUP" --name "$VM_NAME" \
    --command-id RunShellScript --output none --scripts \
    "set -e; curl -fsSL https://get.docker.com | sh; usermod -aG docker $VM_ADMIN; \
     apt-get install -y git cron; curl -sL https://aka.ms/InstallAzureCLIDeb | bash"   # VERIFY on a fresh VM
fi

log "Opening ports 80 and 443 (22 is open from 'az vm create')"
az vm open-port --resource-group "$RESOURCE_GROUP" --name "$VM_NAME" --port 80 --priority 1010 --output none
az vm open-port --resource-group "$RESOURCE_GROUP" --name "$VM_NAME" --port 443 --priority 1020 --output none

VM_IP="$(az vm show --show-details --resource-group "$RESOURCE_GROUP" --name "$VM_NAME" --query publicIps --output tsv)"

cat >&2 <<EOF

Done.
  VM public IP : $VM_IP    (create a DNS A record for your domain pointing here)
  SSH          : ssh $VM_ADMIN@$VM_IP
  Storage      : $STORAGE_ACCOUNT  (containers: uploads, artifacts, outputs, $AZURE_BACKUP_CONTAINER)

Put these lines into .env ON THE VM (do not commit them, do not paste them into chat or tickets):
EOF
printf 'AZURE_STORAGE_CONNECTION_STRING=%s\nAZURE_BACKUP_CONTAINER=%s\n' "$CONNECTION_STRING" "$AZURE_BACKUP_CONTAINER"
