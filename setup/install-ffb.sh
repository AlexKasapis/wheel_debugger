#!/usr/bin/env bash
# Install hid-fanatec (FFB driver for Fanatec CSL Elite Wheel Base) via DKMS.
# Run with sudo (NOT from a bare root shell - the desktop username is taken
# from SUDO_USER so it can be added to the 'games' group):
#     sudo bash setup/install-ffb.sh
# From a root shell, pass it explicitly:
#     REAL_USER=yourname bash setup/install-ffb.sh
set -euo pipefail

VERSION="0.2.3"
DEST="/usr/src/hid-fanatec-${VERSION}"
# Must resolve to the desktop user, not root - this account gets added to the
# 'games' group for sysfs tuning access. Run via `sudo`, not from a root shell.
REAL_USER="${REAL_USER:-${SUDO_USER:-$(logname 2>/dev/null || true)}}"

# Upstream driver source. Vendored under the repo so this is self-contained;
# cloned on first run (vendor/ is gitignored - it is not our code).
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_REPO="${SRC_REPO:-${PROJECT_ROOT}/vendor/hid-fanatecff}"
UPSTREAM="https://github.com/gotzl/hid-fanatecff.git"

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

if [ -z "${REAL_USER}" ] || [ "${REAL_USER}" = "root" ]; then
    echo "!! could not determine the desktop user (got '${REAL_USER}')." >&2
    echo "   Run this with sudo, or set it explicitly:" >&2
    echo "     REAL_USER=yourname bash setup/install-ffb.sh" >&2
    exit 1
fi

echo "==> 0/6  Ensuring driver source at ${SRC_REPO}"
if [ ! -d "${SRC_REPO}" ]; then
    mkdir -p "$(dirname "${SRC_REPO}")"
    git clone --branch "${VERSION}" --depth 1 "${UPSTREAM}" "${SRC_REPO}"
else
    echo "    already present"
fi
[ -f "${SRC_REPO}/dkms.conf" ] || {
    echo "!! ${SRC_REPO} does not look like the hid-fanatecff source" >&2; exit 1; }

echo "==> 1/6  Installing dkms + linuxconsoletools"
zypper --non-interactive install dkms linuxconsoletools

echo "==> 2/6  Staging source at ${DEST}"
if dkms status hid-fanatec 2>/dev/null | grep -q .; then
    echo "    removing previous dkms registration"
    dkms remove hid-fanatec/"${VERSION}" --all 2>/dev/null || true
fi
rm -rf "${DEST}"
mkdir -p "${DEST}"
cp -r "${SRC_REPO}"/. "${DEST}"/
rm -rf "${DEST}/.git"
# clean any artifacts from a previous out-of-tree build so dkms starts fresh
( cd "${DEST}" && rm -f ./*.ko ./*.o ./*.mod ./*.mod.c ./.*.cmd Module.symvers modules.order 2>/dev/null || true )
# dkms.conf and the sources carry a #VERSION# placeholder
find "${DEST}" -type f \( -name dkms.conf -o -name '*.c' \) -exec sed -i "s/#VERSION#/${VERSION}/g" {} +

echo "==> 3/6  Building + installing module via DKMS"
dkms add -m hid-fanatec -v "${VERSION}"
dkms install -m hid-fanatec -v "${VERSION}"

echo "==> 4/6  Installing udev rules"
install -m 0644 "${SRC_REPO}/fanatec.rules" /etc/udev/rules.d/99-fanatec.rules
udevadm control --reload-rules

echo "==> 5/6  Adding ${REAL_USER} to 'games' group (for sysfs tuning access)"
getent group games >/dev/null || groupadd -r games
usermod -aG games "${REAL_USER}"

echo "==> 6/6  Loading module and rebinding the wheel base"
modprobe hid_fanatec
DRV=""
for cand in fanatec hid-fanatec; do
    [ -d "/sys/bus/hid/drivers/${cand}" ] && DRV="${cand}" && break
done
if [ -z "${DRV}" ]; then
    echo "!! module loaded but no matching driver dir under /sys/bus/hid/drivers/" >&2
    ls /sys/bus/hid/drivers/ >&2
    exit 1
fi
echo "    driver registered as: ${DRV}"

# Rebind every Fanatec HID device from hid-generic to the new driver.
for dev in /sys/bus/hid/drivers/hid-generic/*0EB7*; do
    [ -e "${dev}" ] || continue
    id="$(basename "${dev}")"
    echo "    unbinding ${id} from hid-generic"
    echo -n "${id}" > /sys/bus/hid/drivers/hid-generic/unbind || true
    echo "    binding ${id} to ${DRV}"
    echo -n "${id}" > "/sys/bus/hid/drivers/${DRV}/bind" || true
done

udevadm settle || true
echo
echo "==> Done. Current binding:"
ls -l "/sys/bus/hid/drivers/${DRV}/" | grep 0EB7 || echo "    (nothing bound -- try unplugging/replugging the base)"
echo
echo "NOTE: the 'games' group membership needs a re-login to take effect."
