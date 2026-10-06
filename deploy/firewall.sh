#!/usr/bin/env bash
# Host firewall additions for roland-agent (§5.3 / §14.6).
# Default: iptables + DOCKER-USER. Pass --nft for the nftables backend variant.
# Without APPLY=1: dry-run (print planned rules). --install copies the unit/script.
# Idempotent: checks before insert. Preserves SSH/ufw 22/80/443. Tagged roland-agent.
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

mode=iptables
do_install=0
do_apply_rules=1
while [[ $# -gt 0 ]]; do
    case $1 in
        --nft) mode=nft ;;
        --iptables) mode=iptables ;;
        --install) do_install=1 ;;
        --dry-run) APPLY=0 ;;
        --help|-h)
            printf 'Usage: firewall.sh [--iptables|--nft] [--install] [--dry-run]\n'
            printf 'Host mutation requires APPLY=1.\n'
            exit 0
            ;;
        *) die "Unknown argument: $1" ;;
    esac
    shift
done

COMMENT=$deploy_comment
# Egress bridges that must not reach RFC1918/link-local/CGNAT/loopback.
EGRESS_NETS=(10.77.10.0/24 10.77.11.0/24 10.77.12.0/24 10.77.13.0/24)
PRIVATE_NETS=(10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10 127.0.0.0/8)
# Lateral NEW drops (src → dst).
LATERAL=(
    "10.77.3.20 10.77.3.10"
    "10.77.4.40 10.77.4.10"
    "10.77.5.40 10.77.5.30"
    "10.77.6.60 10.77.6.10"
    "10.77.7.70 10.77.7.10"
)
# INPUT drops from egress nets and from service container IPs.
INPUT_NETS=(10.77.11.0/24 10.77.12.0/24 10.77.13.0/24)
INPUT_HOSTS=(10.77.3.20 10.77.4.40 10.77.5.40 10.77.6.60 10.77.7.70)

install_files() {
    require_apply "install firewall script and systemd unit"
    need_cmd sudo
    local dest=/usr/local/sbin/roland-agent-firewall
    local unit_src=$deploy_repo/deploy/roland-agent-firewall.service
    local unit_dst=/etc/systemd/system/roland-agent-firewall.service
    # Install a wrapper that points at the chosen backend.
    local wrapper
    wrapper=$(mktemp)
    cat >"$wrapper" <<WRAP
#!/usr/bin/env bash
set -euo pipefail
export APPLY=1
exec "$deploy_repo/deploy/firewall.sh" --${mode}
WRAP
    chmod 755 -- "$wrapper"
    sudo install -m 0755 -o root -g root -- "$wrapper" "$dest"
    rm -f -- "$wrapper"
    sudo install -m 0644 -o root -g root -- "$unit_src" "$unit_dst"
    sudo systemctl daemon-reload
    sudo systemctl enable --now roland-agent-firewall.service
    printf 'installed %s and enabled roland-agent-firewall.service (%s mode)\n' "$dest" "$mode"
}

iptables_ensure() {
    # Usage: iptables_ensure TABLE CHAIN rule args...
    local table=$1 chain=$2
    shift 2
    if ! is_truthy "${APPLY:-0}"; then
        printf 'would ensure iptables %s %s: %s\n' "$table" "$chain" "$*"
        return 0
    fi
    if sudo iptables -t "$table" -C "$chain" "$@" 2>/dev/null; then
        printf 'keep iptables %s %s: %s\n' "$table" "$chain" "$*"
        return 0
    fi
    sudo iptables -t "$table" -I "$chain" 1 "$@"
    printf 'insert iptables %s %s: %s\n' "$table" "$chain" "$*"
}

apply_iptables() {
    if is_truthy "${APPLY:-0}"; then
        need_cmd iptables
        need_cmd sudo
        if ! sudo iptables -t filter -L DOCKER-USER -n >/dev/null 2>&1; then
            die "DOCKER-USER chain missing. Docker may be using the nftables backend; re-run with --nft."
        fi
    else
        printf 'dry-run iptables rules (APPLY=1 to insert)\n'
    fi

    # 1. ACCEPT RELATED,ESTABLISHED in DOCKER-USER and INPUT.
    iptables_ensure filter DOCKER-USER -m conntrack --ctstate RELATED,ESTABLISHED -m comment --comment "$COMMENT" -j ACCEPT
    iptables_ensure filter INPUT -m conntrack --ctstate RELATED,ESTABLISHED -m comment --comment "$COMMENT" -j ACCEPT

    # 2. DROP NEW from egress nets to private destinations.
    local src dst pair
    for src in "${EGRESS_NETS[@]}"; do
        for dst in "${PRIVATE_NETS[@]}"; do
            iptables_ensure filter DOCKER-USER -s "$src" -d "$dst" -m conntrack --ctstate NEW -m comment --comment "$COMMENT" -j DROP
        done
    done

    # 3. DROP NEW lateral movement.
    for pair in "${LATERAL[@]}"; do
        # shellcheck disable=SC2086
        set -- $pair
        iptables_ensure filter DOCKER-USER -s "$1" -d "$2" -m conntrack --ctstate NEW -m comment --comment "$COMMENT" -j DROP
    done

    # 4. INPUT drops.
    for src in "${INPUT_NETS[@]}"; do
        iptables_ensure filter INPUT -s "$src" -m comment --comment "$COMMENT" -j DROP
    done
    for src in "${INPUT_HOSTS[@]}"; do
        iptables_ensure filter INPUT -s "$src" -m comment --comment "$COMMENT" -j DROP
    done

    printf 'iptables rules reconciled (tag %s). ufw 22/80/443 left unchanged.\n' "$COMMENT"
}

nft_ensure_table() {
    if is_truthy "${APPLY:-0}"; then
        sudo nft list table inet roland-agent >/dev/null 2>&1 || sudo nft add table inet roland-agent
        sudo nft list chain inet roland-agent docker_user >/dev/null 2>&1 || \
            sudo nft add chain inet roland-agent docker_user '{ type filter hook forward priority 0; policy accept; }'
        sudo nft list chain inet roland-agent input >/dev/null 2>&1 || \
            sudo nft add chain inet roland-agent input '{ type filter hook input priority 0; policy accept; }'
    else
        printf 'would ensure nft table inet roland-agent with docker_user + input chains\n'
    fi
}

nft_ensure_rule() {
    local chain=$1
    shift
    local rule=$*
    if is_truthy "${APPLY:-0}"; then
        if sudo nft list chain inet roland-agent "$chain" | grep -F "$COMMENT" | grep -Fq "$rule"; then
            printf 'keep nft %s: %s\n' "$chain" "$rule"
            return 0
        fi
        # shellcheck disable=SC2086
        sudo nft insert rule inet roland-agent "$chain" $rule comment "\"$COMMENT\""
        printf 'insert nft %s: %s\n' "$chain" "$rule"
    else
        printf 'would insert nft %s: %s\n' "$chain" "$rule"
    fi
}

apply_nft() {
    if is_truthy "${APPLY:-0}"; then
        need_cmd nft
        need_cmd sudo
    else
        printf 'dry-run nftables rules (APPLY=1 to insert)\n'
    fi
    nft_ensure_table

    nft_ensure_rule docker_user ct state related,established accept
    nft_ensure_rule input ct state related,established accept

    local src dst pair
    for src in "${EGRESS_NETS[@]}"; do
        for dst in "${PRIVATE_NETS[@]}"; do
            nft_ensure_rule docker_user ip saddr "$src" ip daddr "$dst" ct state new drop
        done
    done
    for pair in "${LATERAL[@]}"; do
        # shellcheck disable=SC2086
        set -- $pair
        nft_ensure_rule docker_user ip saddr "$1" ip daddr "$2" ct state new drop
    done
    for src in "${INPUT_NETS[@]}"; do
        nft_ensure_rule input ip saddr "$src" drop
    done
    for src in "${INPUT_HOSTS[@]}"; do
        nft_ensure_rule input ip saddr "$src" drop
    done
    printf 'nftables rules reconciled (tag %s). ufw 22/80/443 left unchanged.\n' "$COMMENT"
}

if ((do_install)); then
    install_files
fi

case $mode in
    iptables) apply_iptables ;;
    nft) apply_nft ;;
    *) die "Unknown mode: $mode" ;;
esac

if ! is_truthy "${APPLY:-0}"; then
    printf 'dry-run complete. Re-run with APPLY=1 to mutate the host firewall.\n'
    exit 0
fi
