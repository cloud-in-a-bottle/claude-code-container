#!/usr/bin/env bash
#
# Makes podman usable inside this container, then execs the workbench inside the namespaces podman
# needs. entrypoint.sh calls it as `exec /app/nested-podman.sh python3 -m server.app`.
#
# This container is itself a rootless podman container: uid 0 here is an unprivileged host user, and
# openhost runs us with --cap-drop=ALL plus an allowlist that does not include CAP_SYS_ADMIN. Podman
# has to mount things — an overlay per image, a tmpfs or two per container — and mounting needs
# CAP_SYS_ADMIN. A nested user namespace is how to get it without any help from the host: the
# process that creates one holds every capability *inside* it, over the namespaces it owns.
#
# The whole workbench is exec'd into that namespace rather than wrapping the `podman` binary,
# because podman keeps mount state (which layers are mounted, which containers are live) in one
# place: a namespace per invocation would leave `podman run` in one world and `podman ps` in
# another. Exec'ing the server means every terminal it spawns inherits the namespace, so every tab
# and every workspace sees the same podman.
#
# Two more things have to be arranged from in here:
#
#   - Storage. c/storage's overlay driver refuses to stack on overlayfs and our root filesystem is
#     one, so the graph root has to go on something openhost bind-mounts in from the host. It goes
#     on the temp data dir rather than the persistent one: an image store is a cache, and a cache
#     does not belong in a directory that gets backed up.
#   - Cgroups. /sys/fs/cgroup arrives read-only and belongs to the namespace above us, so crun has
#     nowhere to put a container's cgroup. Unsharing a cgroup namespace makes our own cgroup the
#     root of a fresh cgroup2 mount, which we do own and may write.
#
# And one thing cannot be arranged at all, which is why containers here run with --network=host,
# --pid=host and --uts=host (set as defaults in containers.conf below). The kernel will not let a
# process below the initial user namespace mount a fresh procfs or sysfs unless it can already see a
# *fully visible* one, and the podman that runs us masks /proc/acpi, /proc/kcore and /sys/firmware.
# Those masks are locked mounts, so they cannot be undone from in here at any privilege level. A
# container therefore cannot have a procfs of its own; it gets the workbench's /proc bind-mounted
# instead, which is only truthful if it shares the pid namespace too. Sharing the network namespace
# is what makes crun bind-mount /sys rather than mount a fresh sysfs, and is also forced: a network
# namespace of our own could not be connected to anything, since veth and tap both need authority
# over the namespace above us (CAP_NET_ADMIN over it, or /dev/net/tun) and we have neither.
#
# Failures here are logged and stepped over rather than fatal, for the same reason as the rest of
# entrypoint.sh: a workbench without podman is still a workbench, one that won't boot is not.

set -uo pipefail

log() { echo "[podman] $*" >&2; }

# Reads an id map on stdin and writes the ranges it leaves spare for containers, in /etc/subuid
# form. Our own id (0) is not spare: it is the one the workbench's own files belong to.
subordinate_ranges() {
    # The middle field is the id we hold in the namespace above; nothing here needs it, because
    # every id keeps the number it already has.
    local inner _outside count
    while read -r inner _outside count; do
        if [ "$inner" -eq 0 ]; then
            inner=1
            count=$((count - 1))
        fi
        [ "$count" -gt 0 ] && echo "root:$inner:$count"
    done
    return 0
}

rewrite_subordinate_ids() {
    local file=$1 map=$2
    {
        grep -v '^root:' "$file" 2>/dev/null
        subordinate_ranges < "$map"
    } > "$file.workbench" && mv "$file.workbench" "$file"
}

# Both configs are written at every start: the graph root is a runtime path, so neither can be
# baked into the image.
write_configs() {
    local graphroot driver fstype
    # Images and layers are a cache -- recreatable by a re-pull -- so they go in the temp data dir,
    # which openhost does not back up, and never in the persistent one. It still has to be a bind
    # mount from the host: the overlay driver cannot stack on this container's overlayfs root, so
    # falling back to /var/lib means falling back to vfs as well.
    graphroot="${PODMAN_GRAPHROOT:-${OPENHOST_APP_TEMP_DIR:+$OPENHOST_APP_TEMP_DIR/containers/storage}}"
    graphroot="${graphroot:-/var/lib/containers/storage}"
    mkdir -p "$graphroot" || return 1

    # overlay is the driver worth having; vfs is the fallback that works anywhere and copies every
    # layer instead of stacking them, so it is slow and hungry but not wrong.
    fstype=$(stat -f -c %T "$graphroot" 2>/dev/null)
    if [ "$fstype" = "overlayfs" ]; then
        driver=vfs
        log "warning: $graphroot is on overlayfs, which the overlay driver cannot stack on — falling back to vfs"
    else
        driver=overlay
    fi

    mkdir -p /etc/containers/containers.conf.d
    cat > /etc/containers/storage.conf <<EOF
# Written by nested-podman.sh at container start. Edits here are replaced on the next restart.
[storage]
driver = "$driver"
graphroot = "$graphroot"
runroot = "/run/containers/storage"
EOF

    # A drop-in rather than /etc/containers/containers.conf, so the defaults that ship with the
    # distro's containers-common still apply underneath.
    cat > /etc/containers/containers.conf.d/00-workbench.conf <<'EOF'
# Written by nested-podman.sh at container start. Edits here are replaced on the next restart.
[containers]
# Forced, not merely defaulted: see the header of nested-podman.sh. Overriding any of these on a
# `podman run` gets you "OCI permission denied" from crun, not a working container.
netns = "host"
pidns = "host"
utsns = "host"
mounts = ["type=bind,source=/proc,destination=/proc,rw=true"]

[engine]
# No systemd in this container, so neither the systemd cgroup manager nor the journald event logger
# has anything to talk to.
cgroup_manager = "cgroupfs"
events_logger = "file"
runtime = "crun"
EOF

    # What `--userns=auto` and `--uidmap` are allowed to hand out. Derived rather than baked into
    # the image because the range is whatever the host gave this container: a static file would be
    # a lie on a host that maps us differently, and podman would fail at container creation with
    # "not enough unused IDs in user namespace".
    rewrite_subordinate_ids /etc/subuid /proc/self/uid_map
    rewrite_subordinate_ids /etc/subgid /proc/self/gid_map
}

# Only called once we are inside the new namespaces.
mount_writable_cgroups() {
    if mkdir /sys/fs/cgroup/.probe 2>/dev/null; then
        rmdir /sys/fs/cgroup/.probe
        return 0
    fi
    # cgroup2 is already mounted here and the kernel refuses a second mount of the same superblock
    # on its own root, so a tmpfs goes in between to give the new mount somewhere to land. If the
    # cgroup2 mount then fails, take the tmpfs back off: an empty /sys/fs/cgroup would be worse
    # than the read-only one we started with.
    mount -t tmpfs tmpfs /sys/fs/cgroup 2>/dev/null || return 1
    if ! mount -t cgroup2 none /sys/fs/cgroup 2>/dev/null; then
        umount /sys/fs/cgroup 2>/dev/null
        return 1
    fi
}

if [ "${1:-}" = "--inside" ]; then
    shift
    mount_writable_cgroups || log "warning: no writable cgroup tree; podman will not be able to start containers"
    exec "$@"
fi

write_configs || log "warning: could not write /etc/containers config"

# Mirror our own id maps into the child namespace, so that every id we hold keeps the number it
# already has: uid 0 stays uid 0 (the workbench's files stay ours) and the subordinate range the
# host gave us stays available for containers to map. Each extent has to be copied separately —
# the kernel will not accept a child extent that spans two of the parent's.
ns_args=(--setgroups allow --keep-caps --mount --propagation private --cgroup --user)
while read -r inner _outer count; do
    ns_args+=(--map-users "$inner:$inner:$count")
done < /proc/self/uid_map
while read -r inner _outer count; do
    ns_args+=(--map-groups "$inner:$inner:$count")
done < /proc/self/gid_map

# Probed rather than attempted, because a failed `exec unshare` takes the container down with it.
if unshare "${ns_args[@]}" true 2>/dev/null; then
    # unshare(1) execs in place, so the server keeps this pid and stays tini's direct child.
    exec unshare "${ns_args[@]}" "$0" --inside "$@"
fi

log "warning: could not create a nested user namespace; podman will not work in this container"
exec "$@"
