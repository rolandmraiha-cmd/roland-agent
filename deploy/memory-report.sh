#!/usr/bin/env bash
# Read-only M6 memory pre-step and browser load report. No host settings are changed.
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

export LC_ALL=C
fail=0
failure() {
    printf '[FAIL] %s\n' "$*"
    fail=$((fail + 1))
}

integer() {
    [[ $1 =~ ^[0-9]{1,9}$ ]]
}

watch=${WATCH:-0}
browser_cap=${BROWSER_CAP_MIB:-1280}
minimum=${MIN_AVAILABLE_MIB:-800}
if ! integer "$watch" || ! integer "$browser_cap" || ! integer "$minimum"; then
    failure "WATCH, BROWSER_CAP_MIB and MIN_AVAILABLE_MIB must be non-negative integers"
    printf 'PRE-STEP FAIL: no valid memory report\n'
    exit 1
fi
watch=$((10#$watch))
browser_cap=$((10#$browser_cap))
minimum=$((10#$minimum))
if ((browser_cap == 0)); then
    failure "BROWSER_CAP_MIB must be greater than zero"
    printf 'PRE-STEP FAIL: no valid memory report\n'
    exit 1
fi
for command in free docker python3 sleep awk date; do
    if ! command -v "$command" >/dev/null 2>&1; then
        failure "required measurement command unavailable: $command"
        printf 'PRE-STEP FAIL: no valid memory report\n'
        exit 1
    fi
done

clock_now() {
    local stamp
    if ! stamp=$(date +%s 2>/dev/null) || [[ ! $stamp =~ ^[0-9]{1,12}$ ]]; then
        return 1
    fi
    printf '%s\n' "$((10#$stamp))"
}

# Convert Docker's human units without evaluating any returned text as shell code.
memory_bytes() {
    python3 -c '
import re
import sys
from decimal import Decimal, ROUND_CEILING

units = {"B": 1, "kB": 1000, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
         "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
parts = sys.argv[1].split("/")
if len(parts) != 2:
    sys.exit(1)
values = []
for part in parts:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*", part)
    if not match or match[2] not in units:
        sys.exit(1)
    value = int((Decimal(match[1]) * units[match[2]]).to_integral_value(rounding=ROUND_CEILING))
    if not 0 <= value <= 2**60:
        sys.exit(1)
    values.append(value)
print(*values)
' "$1" 2>/dev/null
}

mib() {
    awk -v bytes="$1" 'BEGIN {printf "%.2f", bytes / 1048576}'
}

declare -A peaks=()
minimum_available=""
minimum_projected=""
browser_seen=0
browser_off_seen=0
browser_peak=0
oom_seen=0
elapsed=0

sample() {
    local free_output available rows row service id state oom stats parsed usage limit projected
    local sample_browser=0
    local -A usage_by_service=()
    local -A running_services=()
    printf '\nSample t=%ss\n' "$elapsed"
    if free_output=$(free -m 2>/dev/null); then
        printf '%s\n' "$free_output"
        available=$(printf '%s\n' "$free_output" | awk '$1 == "Mem:" && NF >= 7 {print $7}')
        if integer "$available"; then
            available=$((10#$available))
            if [[ -z $minimum_available ]] || ((available < minimum_available)); then
                minimum_available=$available
            fi
        else
            failure "host available memory could not be measured"
            available=""
        fi
    else
        failure "free -m failed"
        available=""
    fi

    # Compose supplies only service, container ID and state; inspect reads only the OOM flag.
    if ! rows=$(compose ps --all --format '{{.Service}}|{{.ID}}|{{.State}}' 2>/dev/null); then
        failure "Compose container list unavailable"
        return
    fi
    if [[ -z $rows ]]; then
        failure "no Compose containers found"
        return
    fi
    while IFS= read -r row; do
        [[ -n $row ]] || continue
        IFS='|' read -r service id state <<< "$row"
        if [[ ! $service =~ ^[a-zA-Z0-9_.-]+$ || ! $id =~ ^[a-fA-F0-9]{12,64}$ ]]; then
            failure "invalid Compose container metadata"
            continue
        fi
        case $state in
            running | exited | created | restarting | paused | dead | removing) ;;
            *) failure "unknown container state for $service"; continue ;;
        esac
        if [[ $state == running ]]; then
            running_services[$service]=1
        fi
        if ! oom=$(docker inspect --format '{{.State.OOMKilled}}' "$id" 2>/dev/null); then
            failure "OOMKilled unavailable for $service"
            oom=unknown
        fi
        case $oom in
            true)
                oom_seen=1
                printf '[INFO] %s has OOMKilled=true\n' "$service"
                ;;
            false) ;;
            *) failure "unknown OOMKilled flag for $service"; oom=unknown ;;
        esac
        if [[ $state != running ]]; then
            printf 'service=%s state=%s memory=unavailable OOMKilled=%s\n' "$service" "$state" "$oom"
            if [[ $service == browser && $state != exited && $state != created ]]; then
                failure "browser is not in a measurable running or off state"
            fi
            continue
        fi
        if [[ $service == browser ]]; then
            sample_browser=1
            browser_seen=1
        fi
        if ! stats=$(docker stats --no-stream --format '{{.MemUsage}}' "$id" 2>/dev/null); then
            failure "docker stats unavailable for $service"
            continue
        fi
        if ! parsed=$(memory_bytes "$stats"); then
            failure "invalid memory usage or limit for $service"
            continue
        fi
        read -r usage limit <<< "$parsed"
        if ((limit == 0)); then
            failure "memory limit unavailable for $service"
        fi
        # A low reading means nothing if the browser may grow past the cap later: an
        # unlimited container (Docker then reports the host's memory) or a larger limit.
        if [[ $service == browser ]] && ((limit > browser_cap * 1048576)); then
            failure "browser memory limit $(mib "$limit") MiB is above the ${browser_cap} MiB cap"
        fi
        printf 'service=%s state=running memory=%s MiB limit=%s MiB OOMKilled=%s\n' \
            "$service" "$(mib "$usage")" "$(mib "$limit")" "$oom"
        if ((usage > 1152921504606846976 - ${usage_by_service[$service]:-0})); then
            failure "memory usage total out of range for $service"
            continue
        fi
        usage_by_service[$service]=$((${usage_by_service[$service]:-0} + usage))
    done <<< "$rows"

    for service in caddy core model sandbox; do
        if [[ ${running_services[$service]:-0} != 1 ]]; then
            failure "baseline service $service is missing or not running"
        fi
    done

    for service in "${!usage_by_service[@]}"; do
        usage=${usage_by_service[$service]}
        if ((usage > ${peaks[$service]:-0})); then
            peaks[$service]=$usage
        fi
    done
    if ((sample_browser)); then
        usage=${usage_by_service[browser]:-0}
        if ((usage > browser_peak)); then
            browser_peak=$usage
        fi
    else
        browser_off_seen=1
        if [[ -n $available ]]; then
            projected=$((available - browser_cap))
            if [[ -z $minimum_projected ]] || ((projected < minimum_projected)); then
                minimum_projected=$projected
            fi
        fi
    fi
}

if ! started=$(clock_now); then
    failure "observation clock unavailable"
    printf 'PRE-STEP FAIL: no valid memory report\n'
    exit 1
fi
last_clock=$started
next_sample=0
sample
while ((next_sample < watch)); do
    next_sample=$((next_sample + 5))
    if ((next_sample > watch)); then
        next_sample=$watch
    fi
    if ! now=$(clock_now); then
        failure "observation clock unavailable"
        break
    fi
    if ((now < last_clock)); then
        failure "observation clock moved backwards"
        break
    fi
    last_clock=$now
    elapsed=$((now - started))
    if ((elapsed > watch)); then
        break
    fi
    # Docker measurements may take time. Skip missed points instead of extending WATCH.
    while ((next_sample < elapsed && next_sample < watch)); do
        next_sample=$((next_sample + 5))
        if ((next_sample > watch)); then
            next_sample=$watch
        fi
    done
    interval=$((next_sample - elapsed))
    if ((interval > 0)) && ! sleep "$interval"; then
        failure "observation wait failed"
        break
    fi
    if ! now=$(clock_now); then
        failure "observation clock unavailable"
        break
    fi
    if ((now < last_clock)); then
        failure "observation clock moved backwards"
        break
    fi
    last_clock=$now
    elapsed=$((now - started))
    sample
done

printf '\nObserved peaks (MiB):\n'
for service in "${!peaks[@]}"; do
    printf 'service=%s peak=%s MiB\n' "$service" "$(mib "${peaks[$service]}")"
done
if [[ -z $minimum_available ]]; then
    failure "no valid host available-memory measurement"
else
    printf 'minimum host available=%s MiB\n' "$minimum_available"
fi
if ((browser_seen && browser_off_seen)); then
    failure "browser running state changed during observation; repeat with a stable state"
fi
if ((browser_seen)); then
    verdict="A6.5"
    printf 'browser peak=%s MiB; cap=%s MiB; OOM observed=%s\n' "$(mib "$browser_peak")" "$browser_cap" "$oom_seen"
    if ((oom_seen)); then
        failure "OOMKilled observed during browser runtime report"
    fi
    if ((browser_peak > browser_cap * 1048576)); then
        failure "browser peak exceeds cap"
    fi
    if [[ -n $minimum_available ]] && ((minimum_available < minimum)); then
        failure "host available memory fell below ${minimum} MiB"
    fi
else
    verdict="PRE-STEP"
    if [[ -z $minimum_projected ]]; then
        failure "no valid browser-off headroom projection"
    else
        printf 'browser off: projected minimum available=%s MiB after %s MiB browser cap\n' \
            "$minimum_projected" "$browser_cap"
        if ((minimum_projected < minimum)); then
            failure "projected headroom is below ${minimum} MiB"
        fi
    fi
fi
if ((fail > 0)); then
    printf '%s FAIL: %s failed check(s)\n' "$verdict" "$fail"
    exit 1
fi
printf '%s PASS: headroom and observed memory checks passed\n' "$verdict"
