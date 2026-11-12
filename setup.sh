#!/bin/sh
# Configure the signed Runtime Conditions package source without installing software.
set -eu

fail() {
    printf 'Runtime Conditions setup: %s\n' "$*" >&2
    exit 1
}

# Parse the complete installer before executing a script received through a pipe.
main() {
[ "$(id -u)" -eq 0 ] || fail 'Run this script with sudo.'
[ "$(uname -s)" = Linux ] || fail 'Only Linux is supported.'
[ -r /etc/os-release ] || fail 'Cannot identify this Linux distribution.'
. /etc/os-release

case " ${ID:-} ${ID_LIKE:-} " in
    *' debian '*|*' ubuntu '*) family=apt ;;
    *' fedora '*|*' rhel '*|*' centos '*|*' rocky '*|*' almalinux '*|*' ol '*) family=rpm ;;
    *) fail "Unsupported distribution: ${ID:-unknown}." ;;
esac

command -v curl >/dev/null 2>&1 || fail 'curl is required.'
base=https://runtimeconditions.github.io/packages
work=$(mktemp -d)
changed=0
ready=0

cleanup() {
    result=$?
    trap - 0
    if [ "$changed" -eq 1 ] && [ "$ready" -eq 0 ]; then
        [ "$result" -ne 0 ] || result=1
        for item in key source; do
            case "$item" in key) destination=$key_path ;; source) destination=$source_path ;; esac
            if [ -f "$work/$item.previous" ]; then
                cp -p "$work/$item.previous" "$destination"
            else
                rm -f "$destination"
            fi
        done
        printf '%s\n' 'Setup failed; previous source configuration restored.' >&2
    fi
    rm -r "$work"
    exit "$result"
}
trap cleanup 0
trap 'exit 1' HUP INT TERM

if [ "$family" = apt ]; then
    command -v apt-get >/dev/null 2>&1 || fail 'apt-get is required.'
    arch=$(dpkg --print-architecture)
    case "$arch" in amd64|arm64) ;; *) fail "Unsupported architecture: $arch." ;; esac
    key_path=/etc/apt/keyrings/runtimeconditions.gpg
    source_path=/etc/apt/sources.list.d/runtimeconditions.sources
    curl -fsSL --connect-timeout 15 --max-time 120 --retry 2 \
        "$base/keys/runtimeconditions.gpg" -o "$work/key"
    cat > "$work/source" <<EOF
Types: deb
URIs: $base/apt
Suites: stable
Components: main
Architectures: $arch
Signed-By: $key_path
EOF
    install_command='sudo apt install rc-cli'
else
    command -v rpm >/dev/null 2>&1 || fail 'rpm is required.'
    if command -v dnf >/dev/null 2>&1; then
        manager=dnf
    elif command -v yum >/dev/null 2>&1; then
        manager=yum
    else
        fail 'dnf or yum is required.'
    fi
    arch=$(rpm --eval '%{_arch}')
    case "$arch" in x86_64|aarch64) ;; *) fail "Unsupported architecture: $arch." ;; esac
    key_path=/etc/pki/rpm-gpg/RPM-GPG-KEY-runtimeconditions
    source_path=/etc/yum.repos.d/runtimeconditions.repo
    curl -fsSL --connect-timeout 15 --max-time 120 --retry 2 \
        "$base/keys/runtimeconditions.asc" -o "$work/key"
    cat > "$work/source" <<EOF
[runtimeconditions]
name=Runtime Conditions
baseurl=$base/rpm/\$basearch/
enabled=1
skip_if_unavailable=0
gpgcheck=1
repo_gpgcheck=1
gpgkey=file://$key_path
EOF
    install_command="sudo $manager install rc-cli"
fi

[ -s "$work/key" ] || fail 'The signing key download was empty.'
for item in key source; do
    case "$item" in key) destination=$key_path ;; source) destination=$source_path ;; esac
    if [ -e "$destination" ]; then
        [ -f "$destination" ] || fail "Expected a regular file: $destination."
        cp -p "$destination" "$work/$item.previous"
    fi
done

printf 'Configuring Runtime Conditions packages for %s (%s).\n' "${PRETTY_NAME:-$ID}" "$arch"
install -d -m 0755 "$(dirname "$key_path")" "$(dirname "$source_path")"
changed=1
install -m 0644 "$work/key" "$key_path"
install -m 0644 "$work/source" "$source_path"

if [ "$family" = apt ]; then
    apt-get update -o "Dir::Etc::sourcelist=$source_path" -o 'Dir::Etc::sourceparts=-' \
        -o APT::Get::List-Cleanup=0 -o APT::Update::Error-Mode=any </dev/null
else
    rpm --import "$key_path"
    "$manager" --assumeyes --disablerepo='*' --enablerepo=runtimeconditions clean metadata </dev/null
    "$manager" --assumeyes --disablerepo='*' --enablerepo=runtimeconditions makecache </dev/null
fi

ready=1
printf '\nRepository ready. Install the CLI with:\n  %s\n' "$install_command"
}

main
