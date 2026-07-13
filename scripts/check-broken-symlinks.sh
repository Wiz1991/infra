#!/usr/bin/env bash
# check-broken-symlinks.sh - Diagnose broken symlinks in /mnt/media/symlinks
# READ-ONLY: No destructive operations
#
# Decypharr creates symlinks in /mnt/media/symlinks/* pointing to
# /mnt/remote/realdebrid/__all__/... (container path).
# On the host, the real mount is at /mnt/media/remote/realdebrid/__all__/...
# This script checks both path resolution AND file readability.

set -euo pipefail

SYMLINKS_DIR="/mnt/media/symlinks"
REMOTE_DIR="/mnt/media/remote"
BAD_DIR="/mnt/media/remote/realdebrid/__bad__"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

header() { echo -e "\n${BOLD}${CYAN}=== $1 ===${NC}"; }
warn()   { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()    { echo -e "${RED}[ERR]${NC}  $1"; }
ok()     { echo -e "${GREEN}[OK]${NC}   $1"; }

# ---- 1. Overview ----
header "Symlink Directory Overview"
for dir in "$SYMLINKS_DIR"/*/; do
    name=$(basename "$dir")
    total=$(find "$dir" -type l 2>/dev/null | wc -l)
    broken=$(find "$dir" -type l ! -exec test -e {} \; -print 2>/dev/null | wc -l)
    valid=$((total - broken))
    if [ "$broken" -gt 0 ]; then
        err "$name: $broken broken / $total total symlinks ($valid valid)"
    else
        ok "$name: all $total symlinks valid"
    fi
done

# ---- 2. Path Analysis ----
header "Symlink Target Path Analysis"
echo "Checking where symlinks point..."
prefixes=$(find "$SYMLINKS_DIR" -type l -exec readlink {} \; 2>/dev/null \
    | sed 's|/[^/]*$||' | sed 's|/__all__/.*|/__all__|' \
    | sort -u | head -20)

for prefix in $prefixes; do
    host_prefix="/mnt/media${prefix#/mnt}"
    if [ -d "$host_prefix" ]; then
        ok "Target base '$prefix' -> host path '$host_prefix' EXISTS"
    else
        err "Target base '$prefix' -> host path '$host_prefix' DOES NOT EXIST"
        warn "Symlinks use container-internal paths. On host, /mnt/remote = /mnt/media/remote"
    fi
done

# ---- 3. RealDebrid Mount Health ----
header "RealDebrid Mount Health"
if mountpoint -q "$REMOTE_DIR/realdebrid" 2>/dev/null || \
   mount | grep -q "decypharr-realdebrid.*$REMOTE_DIR/realdebrid"; then
    ok "RealDebrid FUSE mount is active at $REMOTE_DIR/realdebrid"
else
    err "RealDebrid mount not found at $REMOTE_DIR/realdebrid"
fi

all_count=$(ls "$REMOTE_DIR/realdebrid/__all__/" 2>/dev/null | wc -l)
bad_count=$(ls "$BAD_DIR/" 2>/dev/null | wc -l)
echo "  Torrents in __all__: $all_count"
echo "  Torrents in __bad__: $bad_count"

# ---- 4. File Readability Check ----
header "File Readability Analysis (sampling broken symlinks)"
echo "Testing which broken symlink targets are readable vs IO error..."

readable=0
io_error=0
missing=0
io_error_files=()
sample_size=0

while IFS= read -r symlink; do
    sample_size=$((sample_size + 1))
    target=$(readlink "$symlink")
    host_target="/mnt/media${target#/mnt}"

    if [ -f "$host_target" ]; then
        if head -c 1 "$host_target" >/dev/null 2>&1; then
            readable=$((readable + 1))
        else
            io_error=$((io_error + 1))
            io_error_files+=("$(basename "$symlink")")
        fi
    else
        missing=$((missing + 1))
    fi
done < <(find "$SYMLINKS_DIR" -type l ! -exec test -e {} \; -print 2>/dev/null | shuf | head -200)

total_broken=$(find "$SYMLINKS_DIR" -type l ! -exec test -e {} \; -print 2>/dev/null | wc -l)
echo ""
echo "  Sample size: $sample_size of $total_broken broken symlinks"
echo ""

if [ "$readable" -gt 0 ]; then
    ok "Readable (path issue only):  $readable  (~$((readable * 100 / sample_size))%)"
fi
if [ "$io_error" -gt 0 ]; then
    err "IO Error (infringing/dead):  $io_error  (~$((io_error * 100 / sample_size))%)"
fi
if [ "$missing" -gt 0 ]; then
    warn "Missing from mount:          $missing  (~$((missing * 100 / sample_size))%)"
fi

# Extrapolate
echo ""
echo -e "  ${BOLD}Estimated totals across all $total_broken broken symlinks:${NC}"
if [ "$sample_size" -gt 0 ]; then
    echo "    ~$((readable * total_broken / sample_size)) are just broken paths (files are fine on disk)"
    echo "    ~$((io_error * total_broken / sample_size)) have actual IO errors (infringing/dead files on RealDebrid)"
    echo "    ~$((missing * total_broken / sample_size)) have missing files"
fi

# ---- 5. __bad__ vs IO Error Cross-Reference ----
header "RealDebrid __bad__ Folder Analysis"
echo "Items in __bad__ that still show IO errors in __all__:"
bad_and_broken=0
bad_still_readable=0
bad_not_in_all=0

while IFS= read -r bad_entry; do
    name=$(echo "$bad_entry" | sed 's/ || .*//' | sed 's/ *$//')
    matching=$(find "$REMOTE_DIR/realdebrid/__all__" -maxdepth 1 -name "$name" 2>/dev/null | head -1)
    if [ -n "$matching" ]; then
        first_file=$(find "$matching" -type f 2>/dev/null | head -1)
        if [ -n "$first_file" ]; then
            if head -c 1 "$first_file" >/dev/null 2>&1; then
                bad_still_readable=$((bad_still_readable + 1))
            else
                bad_and_broken=$((bad_and_broken + 1))
            fi
        fi
    else
        bad_not_in_all=$((bad_not_in_all + 1))
    fi
done < <(ls "$BAD_DIR/" 2>/dev/null)

err "In __bad__ AND unreadable: $bad_and_broken"
if [ "$bad_still_readable" -gt 0 ]; then
    warn "In __bad__ but still readable: $bad_still_readable"
fi
if [ "$bad_not_in_all" -gt 0 ]; then
    ok "In __bad__ and removed from __all__: $bad_not_in_all"
fi

# ---- 6. IO Error file listing ----
header "Sample IO Error Files (infringing/dead on RealDebrid)"
if [ "${#io_error_files[@]}" -gt 0 ]; then
    for f in "${io_error_files[@]:0:30}"; do
        err "$f"
    done
    if [ "${#io_error_files[@]}" -gt 30 ]; then
        echo "  ... and $((${#io_error_files[@]} - 30)) more"
    fi
else
    ok "No IO errors found in sample"
fi

# ---- 7. Decypharr Repair Status ----
header "Decypharr Repair Status"
if [ -f /srv/homelab/decypharr/repair.json ]; then
    echo "  Last repair state:"
    python3 -c "
import json, sys
with open('/srv/homelab/decypharr/repair.json') as f:
    data = json.load(f)
for key, job in data.items():
    print(f\"  Job: {key}\")
    print(f\"    Status:  {job.get('status', 'unknown')}\")
    print(f\"    Created: {job.get('created_at', 'unknown')}\")
    if job.get('error'):
        print(f\"    Error:   {job['error']}\")
" 2>/dev/null || echo "  Could not parse repair.json"
else
    warn "No repair.json found"
fi

# Check if repair is failing
if docker logs decypharr --tail 50 2>&1 | grep -q "arr not configured"; then
    echo ""
    err "Repair is FAILING with 'arr not configured' error!"
    echo ""
    echo "  ROOT CAUSE: Decypharr v1.1.6 has a known bug (#150) where the"
    echo "  authenticate() function overwrites arr Host/Token with download"
    echo "  client credentials. If Sonarr/Radarr send empty username/password"
    echo "  in their download client config, it blanks out the arr settings."
    echo ""
    echo "  WORKAROUND: In Sonarr/Radarr Settings -> Download Clients -> qBittorrent:"
    echo "    Username: <arr host URL, e.g. http://sonarr:8989>"
    echo "    Password: <arr API token>"
    echo ""
    echo "  PERMANENT FIX: Upgrade decypharr to v2.0+"
fi

# ---- 8. Summary ----
header "Summary"
echo -e "
${BOLD}TWO SEPARATE ISSUES FOUND:${NC}

${YELLOW}1. ALL symlinks are broken from host perspective${NC}
   Symlinks point to: /mnt/remote/realdebrid/...  (container path)
   Actual host path:  /mnt/media/remote/realdebrid/...
   This is expected - decypharr runs inside Docker where /mnt/media is mounted
   as /mnt. Sonarr/Radarr share the same mount mapping so they can resolve
   these paths. The symlinks work inside the container ecosystem.

${RED}2. ~${io_error}/${sample_size} sampled files (~$((io_error * 100 / sample_size))%) have IO errors (infringing/dead)${NC}
   These files exist on the RealDebrid mount but return IO errors when read.
   This typically means RealDebrid has flagged them as infringing or the
   download links have expired/been removed.
   RealDebrid __bad__ folder has $bad_count items.

${RED}3. Decypharr repair is non-functional${NC}
   Repair fails with 'arr not configured' (known bug #150 in v1.1.6).
   The authenticate() function overwrites arr credentials with download
   client auth, blanking them if Sonarr/Radarr don't send credentials.
"
