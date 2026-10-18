#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# capacity_hunter.sh - fight the infamous "Out of capacity" error for
# Always Free Ampere A1 instances.
#
# Strategy: retry instance creation in a loop, rotating availability domains
# and (optionally) degrading the shape (4/12 -> 2/12 -> 1/6) so that you get
# *something* running. Runs anywhere with the OCI CLI configured, which
# includes OCI Cloud Shell out of the box.
#
# Prereqs:
#   oci CLI logged in (Cloud Shell: nothing to do; local: `oci session authenticate` or API keys)
#   jq
#
# Usage:
#   ./scripts/capacity_hunter.sh \
#     --compartment ocid1.compartment... \
#     --ssh-key ~/.ssh/id_ed25519.pub \
#     --subnet ocid1.subnet...          # optional: reuse terraform's subnet
#     --shape-cpus 2 --shape-memory 12  # default 2/12 (post-2026 free limit)
#     --fallbacks "2,12;1,6"           # shapes tried in order after primary fails
#     --interval 30
#
# Without --subnet the script creates/uses a hunter-specific VCN+subnet+IGW
# so it works completely standalone (then terraform import or just use it).
# ---------------------------------------------------------------------------
set -uo pipefail

COMPARTMENT=""
SSH_KEY_FILE=""
SUBNET=""
SHAPE_CPUS=2
SHAPE_MEMORY=12
FALLBACKS="2,12;1,6"
INTERVAL=30
REGION="${OCI_CLI_REGION:-}"
PREFIX="gh-runner-hunt"

log()  { printf '\033[1;36m[hunt]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[hunt]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[hunt]\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --compartment) COMPARTMENT="$2"; shift 2 ;;
    --ssh-key)     SSH_KEY_FILE="$2"; shift 2 ;;
    --subnet)      SUBNET="$2"; shift 2 ;;
    --shape-cpus)  SHAPE_CPUS="$2"; shift 2 ;;
    --shape-memory) SHAPE_MEMORY="$2"; shift 2 ;;
    --fallbacks)   FALLBACKS="$2"; shift 2 ;;
    --interval)    INTERVAL="$2"; shift 2 ;;
    --region)      REGION="$2"; shift 2 ;;
    *) die "unknown option: $1" ;;
  esac
done

command -v oci >/dev/null || die "oci CLI not found"
command -v jq >/dev/null  || die "jq not found"
[[ -n "$COMPARTMENT" ]] || die "--compartment is required (ocid1.compartment...)"
[[ -n "$SSH_KEY_FILE" ]] || die "--ssh-key is required (path to a .pub)"
SSH_KEY="$(cat "$SSH_KEY_FILE")"
SSH_KEY_FILE="$(realpath "$SSH_KEY_FILE")"

# ---------------------------------------------------------------- networking
if [[ -z "$SUBNET" ]]; then
  log "ensuring hunt VCN/subnet..."
  VCN_ID="$(oci network vcn list -c "$COMPARTMENT" --all \
      --query "data [?\"display-name\"=='${PREFIX}-vcn'].id | [0]" --raw-output 2>/dev/null || true)"
  if [[ -z "$VCN_ID" || "$VCN_ID" == "null" ]]; then
    VCN_ID="$(oci network vcn create -c "$COMPARTMENT" \
        --display-name "${PREFIX}-vcn" --cidr-blocks '["10.9.0.0/16"]' \
        --query 'data.id' --raw-output)" || die "cannot create VCN"
    IGW_ID="$(oci network internet-gateway create -c "$COMPARTMENT" \
        --vcn-id "$VCN_ID" --display-name "${PREFIX}-igw" --is-enabled true \
        --query 'data.id' --raw-output)"
    RT_ID="$(oci network route-table create -c "$COMPARTMENT" \
        --vcn-id "$VCN_ID" --display-name "${PREFIX}-rt" \
        --route-rules "[{\"cidrBlock\":\"0.0.0.0/0\",\"networkEntityId\":\"$IGW_ID\"}]" \
        --query 'data.id' --raw-output)"
    SUBNET="$(oci network subnet create -c "$COMPARTMENT" \
        --vcn-id "$VCN_ID" --display-name "${PREFIX}-subnet" --cidr-block "10.9.1.0/24" \
        --route-table-id "$RT_ID" --query 'data.id' --raw-output)"
  else
    SUBNET="$(oci network subnet list -c "$COMPARTMENT" --vcn-id "$VCN_ID" --all \
        --query "data [?\"display-name\"=='${PREFIX}-subnet'].id | [0]" --raw-output)"
    if [[ -z "$SUBNET" || "$SUBNET" == "null" ]]; then
      IGW_ID="$(oci network internet-gateway list -c "$COMPARTMENT" --vcn-id "$VCN_ID" --all \
          --query "data [?\"display-name\"=='${PREFIX}-igw'].id | [0]" --raw-output)"
      RT_ID="$(oci network route-table create -c "$COMPARTMENT" \
          --vcn-id "$VCN_ID" --display-name "${PREFIX}-rt-2" \
          --route-rules "[{\"cidrBlock\":\"0.0.0.0/0\",\"networkEntityId\":\"$IGW_ID\"}]" \
          --query 'data.id' --raw-output)"
      SUBNET="$(oci network subnet create -c "$COMPARTMENT" --vcn-id "$VCN_ID" \
          --display-name "${PREFIX}-subnet" --cidr-block "10.9.2.0/24" \
          --route-table-id "$RT_ID" --query 'data.id' --raw-output)"
    fi
  fi
  log "subnet: $SUBNET"
fi

# ------------------------------------------------------------------- image
log "finding latest Ubuntu 24.04 arm64 image..."
IMAGE_ID="$(oci compute image list -c "$COMPARTMENT" --operating-system "Canonical Ubuntu" \
    --operating-system-version 24.04 --shape VM.Standard.A1.Flex --all \
    --sort-by TIMECREATED --sort-order DESC \
    --query "data [?starts_with(\"display-name\", 'Canonical-Ubuntu-24.04-2')].id | [0]" \
    --raw-output)"
[[ -n "$IMAGE_ID" && "$IMAGE_ID" != "null" ]] || die "no Ubuntu 24.04 arm64 image found"
log "image: $IMAGE_ID"

ADS="$(oci iam availability-domain list -c "$COMPARTMENT" --query 'data[].name' --raw-output | jq -r '.[]' | paste -sd'|')"
[[ -n "$ADS" ]] || die "cannot list availability domains"
mapfile -t AD_LIST < <(echo "$ADS" | tr '|' '\n')
log "availability domains: ${AD_LIST[*]}"

# -------------------------------------------------------------------- hunt
attempt=0
ad_index=0
shape_list="${SHAPE_CPUS},${SHAPE_MEMORY};${FALLBACKS}"
IFS=';' read -r -a SHAPES <<< "$shape_list"

log "hunting with shape ladder: ${SHAPES[*]} (Ctrl-C to stop; each attempt ~$INTERVAL s apart)"

while true; do
  for shape in "${SHAPES[@]}"; do
    CPUS="${shape%%,*}"
    MEM="${shape##*,}"
    AD="${AD_LIST[$((ad_index % ${#AD_LIST[@]}))]}"
    ((attempt+=1))
    log "attempt #$attempt shape=${CPUS}c/${MEM}g AD=${AD}"
    OUT="$(oci compute instance launch \
        -c "$COMPARTMENT" \
        --availability-domain "$AD" \
        --display-name "${PREFIX}-$(date +%s)" \
        --shape VM.Standard.A1.Flex \
        --shape-config "{\"ocpus\": ${CPUS}, \"memoryInGBs\": ${MEM}}" \
        --image-id "$IMAGE_ID" \
        --subnet-id "$SUBNET" \
        --assign-public-ip true \
        --ssh-authorized-keys-file "$SSH_KEY_FILE" \
        --wait-for-state RUNNING 2>&1 || true)"

    if echo "$OUT" | grep -qi "Out of host capacity\|out of capacity"; then
      warn "out of capacity (${CPUS}c/${MEM}g @ ${AD})"
    elif echo "$OUT" | grep -qi "LimitExceeded\|quota"; then
      warn "service limit reached for ${CPUS} OCPU - trying a smaller shape"
      continue
    elif echo "$OUT" | grep -q "ocid1.instance"; then
      INSTANCE_ID="$(echo "$OUT" | grep -o 'ocid1\.instance\.[a-z0-9.]*' | head -1)"
      log "SUCCESS! instance provisioning: $INSTANCE_ID"
      sleep 45
      IP="$(oci compute instance get --instance-id "$INSTANCE_ID" \
          --query 'data."primary-vnic".attributes."public-ip"' --raw-output 2>/dev/null || true)"
      log "instance running. OCID: $INSTANCE_ID"
      log "public IP: ${IP:-unknown (check console)}"
      log "ssh: ssh -i ${SSH_KEY_FILE%.pub} ubuntu@${IP:-<ip>}"
      exit 0
    else
      warn "unrecognised response: $(echo "$OUT" | tail -2)"
    fi
  done
  ((ad_index+=1))
  sleep "$INTERVAL"
done
