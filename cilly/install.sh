#!/bin/sh
# cilly's one-time setup that needs root.  Read it before you run it.
#
#   sudo sh install.sh            asks once, then does the steps below
#   sh install.sh --dry-run       only prints what it would do (no root needed)
#
# It does these things, and nothing else:
#   1. installs QEMU, bubblewrap, xorriso and openssl (and virtiofsd if your distribution has it)
#      from your distribution's own packages, skipping what's there already
#   2. adds your user to the kvm group, if you can't use /dev/kvm yet
#   3. on Ubuntu 23.10 and newer, when bubblewrap can't create the user namespaces its
#      sandbox needs, turns on Ubuntu's own AppArmor profile for it, bwrap-userns-restrict
#      (it never writes a profile of its own, and replaces one an older cilly wrote)
#
# Everything else cilly sets up as you, in its web page.  Options:
#   --yes          don't ask before going ahead
#   --user NAME    the user who runs cilly (default: the one who ran sudo)
#   --dry-run      print the steps and the profile, and check bubblewrap, but change nothing
set -eu

DRY=0
YES=0
TARGET="${SUDO_USER:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --yes|-y) YES=1 ;;
    --user)
      if [ $# -lt 2 ] || [ -z "$2" ]; then echo "--user needs a user name" >&2; exit 2; fi
      shift; TARGET="$1" ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
OS_RELEASE=/etc/os-release
USERNS=/proc/sys/kernel/apparmor_restrict_unprivileged_userns
UBUNTU_PROFILE=/usr/share/apparmor/extra-profiles/bwrap-userns-restrict
APPARMOR_D=/etc/apparmor.d
# The tests point these at their own files.  Only a dry run, which changes nothing, takes them:
# with sudo -E they would come from your environment into a script that runs as root.
if [ "$DRY" = 1 ]; then
  OS_RELEASE="${CILLY_OS_RELEASE:-$OS_RELEASE}"
  USERNS="${CILLY_USERNS_SYSCTL:-$USERNS}"
  UBUNTU_PROFILE="${CILLY_UBUNTU_BWRAP_PROFILE:-$UBUNTU_PROFILE}"
  APPARMOR_D="${CILLY_APPARMOR_D:-$APPARMOR_D}"
fi
PROFILE="$APPARMOR_D/bwrap-userns-restrict"
OLD_PROFILE="$APPARMOR_D/cilly-bwrap"  # what older versions of this script wrote

say() { printf '%s\n' "$*"; }
run() {
  say "+ $*"
  if [ "$DRY" = 0 ]; then "$@"; fi
}

# --- which distribution ------------------------------------------------------
ID="" ID_LIKE="" PRETTY_NAME=""
if [ -r "$OS_RELEASE" ]; then
  # shellcheck disable=SC1090
  . "$OS_RELEASE"
fi
FAMILY=""
for id in $ID $ID_LIKE; do  # RHEL and its rebuilds say "fedora" in ID_LIKE, so they come first
  case "$id" in rhel|centos|rocky|almalinux|ol) FAMILY=rhel ;; esac
done
if [ -z "$FAMILY" ]; then
  for id in $ID $ID_LIKE; do
    case "$id" in
      arch|archarm|manjaro|endeavouros) FAMILY=arch ;;
      debian|ubuntu) FAMILY=debian ;;
      fedora) FAMILY=fedora ;;
      suse|opensuse*) FAMILY=suse ;;
    esac
    [ -n "$FAMILY" ] && break
  done
fi

# command:package, the same packages as cilly/host.py (a test keeps them equal).  A package
# whose command is there already is skipped: on Arch, qemu-full or qemu-desktop already has QEMU.
case "$FAMILY" in
  arch)   NEED="python3:python qemu-system-x86_64:qemu-base qemu-img:qemu-base xorriso:libisoburn bwrap:bubblewrap openssl:openssl" ;;
  debian) NEED="python3:python3 qemu-system-x86_64:qemu-system-x86 qemu-img:qemu-utils xorriso:xorriso bwrap:bubblewrap openssl:openssl" ;;
  fedora) NEED="python3:python3 qemu-system-x86_64:qemu-system-x86-core qemu-img:qemu-img xorriso:xorriso bwrap:bubblewrap openssl:openssl" ;;
  suse)   NEED="python3:python3 qemu-system-x86_64:qemu-x86 qemu-img:qemu-tools xorriso:xorriso bwrap:bubblewrap openssl:openssl" ;;
  rhel)
    say "${PRETTY_NAME:-This system} isn't supported yet. RHEL, CentOS Stream, Rocky Linux and AlmaLinux"
    say "have QEMU as qemu-kvm in /usr/libexec, which cilly hasn't been tried with. Nothing changed."
    exit 1 ;;
  *)
    say "cilly doesn't know how to install packages on ${PRETTY_NAME:-this system}. Nothing changed."
    say "Install these yourself: Python 3.11 or newer, QEMU (qemu-system-x86_64 and qemu-img),"
    say "bubblewrap, xorriso and openssl. Then make sure you can read and write /dev/kvm."
    exit 1 ;;
esac
OPTIONAL="virtiofsd"

PKGS=""
for pair in $NEED; do
  cmd="${pair%%:*}" pkg="${pair#*:}"
  command -v "$cmd" >/dev/null 2>&1 && continue
  case " $PKGS " in *" $pkg "*) ;; *) PKGS="${PKGS:+$PKGS }$pkg" ;; esac
done
for p in /usr/lib/virtiofsd /usr/libexec/virtiofsd /usr/lib/qemu/virtiofsd; do
  [ -x "$p" ] && OPTIONAL=""
done
command -v virtiofsd >/dev/null 2>&1 && OPTIONAL=""

UPDATED=""
install_pkgs() {  # $@: packages
  case "$FAMILY" in
    arch)   run pacman -S --needed --noconfirm "$@" ;;
    debian)  # the package lists can be empty or old, as on a fresh cloud image
      if [ -z "$UPDATED" ]; then run apt-get update; UPDATED=1; fi
      run env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
    fedora) run dnf install -y "$@" ;;
    suse)   run zypper --non-interactive install "$@" ;;
  esac
}

# --- what's needed -----------------------------------------------------------
if [ "$DRY" = 0 ] && [ "$(id -u)" != 0 ]; then
  say "This needs root. Run it with sudo: sudo sh $0"
  exit 1
fi
if [ -z "$TARGET" ] || [ "$TARGET" = root ]; then
  if [ "$DRY" = 1 ]; then TARGET="${TARGET:-$(id -un)}"; else
    say "Which user runs cilly? Run this with sudo from your own account, or pass --user NAME."
    exit 1
  fi
fi
if ! id -u "$TARGET" >/dev/null 2>&1; then
  say "There's no user called $TARGET here. Nothing changed."
  exit 1
fi

KVM_STEP=""
if [ ! -e /dev/kvm ]; then
  KVM_NOTE="/dev/kvm doesn't exist. Turn on virtualization (Intel VT-x or AMD-V) in your firmware settings."
elif [ "$(stat -c %a /dev/kvm)" = 666 ]; then
  KVM_NOTE="Everyone can use /dev/kvm here already."
elif id -nG "$TARGET" 2>/dev/null | tr ' ' '\n' | grep -qx kvm; then
  KVM_NOTE="$TARGET is in the kvm group already."
else
  KVM_STEP=1
  KVM_NOTE="Add $TARGET to the kvm group, so cilly can start VMs. Log out and back in afterwards."
fi

# cilly never writes an AppArmor profile of its own.  One that only allows user namespaces would
# let every program that runs bubblewrap have them, where Ubuntu's own keeps what runs inside confined.
APPARMOR_STEP=""
APPARMOR_NOTE=""
REPLACE_OLD=""
if [ "$(cat "$USERNS" 2>/dev/null || true)" = 1 ]; then
  if [ -f "$UBUNTU_PROFILE" ] && [ -f "$OLD_PROFILE" ]; then
    APPARMOR_STEP=1
    REPLACE_OLD=1
    APPARMOR_NOTE="Replace the AppArmor profile an older cilly wrote for bubblewrap ($OLD_PROFILE), which lets every program that runs bubblewrap create user namespaces, with Ubuntu's own (bwrap-userns-restrict)."
  elif [ -f "$UBUNTU_PROFILE" ]; then
    APPARMOR_STEP=1
    APPARMOR_NOTE="Unless bubblewrap works already, turn on Ubuntu's own AppArmor profile for it (bwrap-userns-restrict), so it can create user namespaces."
  elif [ -f "$OLD_PROFILE" ]; then
    APPARMOR_NOTE="An older cilly wrote an AppArmor profile for bubblewrap ($OLD_PROFILE). It lets every program that runs bubblewrap create user namespaces. This Ubuntu has no profile of its own to put in its place, so it stays. To remove it, run apparmor_parser -R $OLD_PROFILE, then rm $OLD_PROFILE, and set vm.use_bwrap to \"off\" in config.json."
  else
    APPARMOR_NOTE="This Ubuntu has no AppArmor profile for bubblewrap (bwrap-userns-restrict) to turn on, and cilly doesn't write one of its own. If cilly's setup page says bubblewrap is blocked, update Ubuntu, or set vm.use_bwrap to \"off\" in config.json."
  fi
fi

say "cilly's one-time setup for ${PRETTY_NAME:-$FAMILY}, for the user $TARGET:"
if [ -n "$PKGS" ]; then
  say "  1. Install $PKGS${OPTIONAL:+, and $OPTIONAL if there is a package for it}."
elif [ -n "$OPTIONAL" ]; then
  say "  1. The packages cilly needs are there. Install $OPTIONAL if there is a package for it."
else
  say "  1. The packages cilly needs are there already."
fi
say "  2. $KVM_NOTE"
[ -n "$APPARMOR_NOTE" ] && say "  3. $APPARMOR_NOTE"
say ""

if [ "$DRY" = 0 ] && [ "$YES" = 0 ]; then
  if [ ! -t 0 ]; then say "Run it again with --yes to go ahead without asking."; exit 1; fi
  printf 'Go ahead? [y/N] '
  read -r answer
  case "$answer" in y|Y|yes|Yes) ;; *) say "Nothing changed."; exit 1 ;; esac
fi

# --- 1. packages --------------------------------------------------------------
if [ -n "$PKGS" ]; then
  install_pkgs $PKGS
fi
if [ -n "$OPTIONAL" ] && ! install_pkgs $OPTIONAL; then
  say "No $OPTIONAL package here. cilly works without it, only live folder shares need it."
fi

# --- 2. /dev/kvm ---------------------------------------------------------------
if [ -n "$KVM_STEP" ]; then
  getent group kvm >/dev/null 2>&1 || run groupadd --system kvm
  run usermod -aG kvm "$TARGET"
fi

# --- 3. AppArmor ---------------------------------------------------------------
# Decided now that bubblewrap is installed: nothing to do when it works for $TARGET already, and
# never a second profile for it (two profiles that attach to one program break it).
bwrap_works() {  # the same check cilly makes before it starts a VM
  if [ "$(id -u)" = 0 ]; then
    command -v runuser >/dev/null 2>&1 &&
      runuser -u "$TARGET" -- "$BWRAP" --unshare-all --die-with-parent --ro-bind / / -- true >/dev/null 2>&1
  elif [ "$(id -un)" = "$TARGET" ]; then
    "$BWRAP" --unshare-all --die-with-parent --ro-bind / / -- true >/dev/null 2>&1
  else
    return 1
  fi
}
other_profile() {  # an enabled profile for bubblewrap besides Ubuntu's and the old one of cilly's
  for f in $(grep -lsE "^[[:space:]]*(profile[[:space:]]+[^[:space:]]+[[:space:]]+)?$BWRAP[[:space:]]" "$APPARMOR_D"/* 2>/dev/null); do
    [ "$f" = "$PROFILE" ] || [ "$f" = "$OLD_PROFILE" ] || [ -e "$APPARMOR_D/disable/$(basename "$f")" ] || { echo "$f"; return 0; }
  done
  return 0
}
if [ -n "$APPARMOR_STEP" ]; then
  BWRAP="$(command -v bwrap || true)"
  OTHER=""
  if [ -z "$BWRAP" ] && [ "$DRY" = 1 ]; then
    BWRAP=/usr/bin/bwrap
    say "bubblewrap isn't installed yet, so this can't check it. After the packages, it would:"
  elif [ -z "$BWRAP" ]; then
    APPARMOR_STEP=""
    say "bubblewrap isn't installed, so it needs no AppArmor profile."
  elif [ -z "$REPLACE_OLD" ] && bwrap_works; then
    APPARMOR_STEP=""
    say "bubblewrap works for $TARGET already, so it needs no AppArmor profile."
  else
    OTHER="$(other_profile)"
  fi
  if [ -n "$OTHER" ]; then  # two profiles for one program break it, so this never adds a second one
    APPARMOR_STEP=""
    say "AppArmor has a profile for bubblewrap already ($OTHER), so this adds none. But bubblewrap"
    say "can't create user namespaces for $TARGET with it. Check that profile (sudo aa-status lists"
    say "the loaded ones), or set vm.use_bwrap to \"off\" in config.json."
  elif [ -n "$APPARMOR_STEP" ] && [ -n "$REPLACE_OLD" ]; then
    say "Replacing the older profile of cilly's with Ubuntu's own:"
  elif [ -n "$APPARMOR_STEP" ]; then
    say "bubblewrap can't create user namespaces for $TARGET, so it gets Ubuntu's AppArmor profile:"
  fi
fi
if [ -n "$APPARMOR_STEP" ]; then
  if [ -n "$REPLACE_OLD" ]; then
    run apparmor_parser -R "$OLD_PROFILE"
    run rm -f "$OLD_PROFILE"
  fi
  run cp "$UBUNTU_PROFILE" "$PROFILE"
  [ "$DRY" = 1 ] && sed 's/^/    /' "$UBUNTU_PROFILE"
  run apparmor_parser -r "$PROFILE"
  if [ "$DRY" = 0 ]; then
    if bwrap_works; then say "bubblewrap works now."; else say "bubblewrap still can't build its sandbox. cilly's setup page says why."; fi
  fi
  say "To undo it: first apparmor_parser -R $PROFILE, then rm $PROFILE."
fi

# --- done -----------------------------------------------------------------------
py_new() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; }
PY=python3
if ! py_new python3; then  # Ubuntu 22.04's is 3.10: a newer one may be installed beside it
  PY=""
  for v in 3.14 3.13 3.12 3.11; do
    if command -v "python$v" >/dev/null 2>&1 && py_new "python$v"; then PY="python$v"; break; fi
  done
fi
say ""
if [ -z "$PY" ]; then
  say "cilly needs Python 3.11 or newer, and python3 here is older. Install a newer one"
  say "(on Ubuntu 22.04: apt install python3.11) and start cilly with it."
  PY=python3.11
elif [ "$PY" != python3 ]; then
  say "python3 here is older than 3.11, so start cilly with $PY."
fi
[ -n "$KVM_STEP" ] && say "Log out and back in, so $TARGET's new kvm group applies."
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
if [ -f "$HERE/../pyproject.toml" ]; then
  say "Done. Now, as $TARGET and without sudo: cd $(dirname "$HERE") && $PY -m cilly setup"
else
  say "Done. Now, as $TARGET and without sudo: cilly setup"
fi
