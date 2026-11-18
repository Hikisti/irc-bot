#!/bin/bash
# Fetches a game night's files from the server into tools/logs/ with one SSH login (run on the Mac,
# from anywhere): the nightcheck.sh output, the bot's raw journal for the window, and with --probe /
# --sampler the goal probe's / Liiga sampler's log. Then `/post-game NAME` in Claude Code reads them.
#
#   tools/fetch_night.sh NAME "FROM" "TO" [--probe] [--sampler] [--dry-run]
#   tools/fetch_night.sh 2026-11-19_nhl "2026-11-18 23:30" "2026-11-19 07:30" --probe
#
# NAME labels the files (tools/logs/journal_NAME.log, nightcheck_NAME.log); FROM and TO are the
# journal window in UTC. The server's address, the bot's unit and its checkout come from
# tools/fetch_night.conf, which is gitignored: this repository is public, so nothing about the
# server is written here. The fetched files hold chat and addresses; *.log keeps them out of git.
set -euo pipefail
cd "$(dirname "$0")/.."

usage() { sed -n '6,7p' "$0" | sed 's/^# *//' >&2; exit 2; }
[ $# -ge 3 ] || usage
name=$1 from=$2 to=$3
shift 3
probe=0 sampler=0 dry_run=0
for arg in "$@"; do
    case $arg in
        --probe) probe=1 ;;
        --sampler) sampler=1 ;;
        --dry-run) dry_run=1 ;;
        *) usage ;;
    esac
done
# The values go into a command line run on the server, so only these shapes are accepted.
[[ $name =~ ^[A-Za-z0-9_-]+$ ]] || { echo "NAME may hold only letters, digits, - and _" >&2; exit 2; }
for t in "$from" "$to"; do
    [[ $t =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}$ ]] || { echo "Times look like \"2026-11-19 07:30\"" >&2; exit 2; }
done

conf=tools/fetch_night.conf
[ -f "$conf" ] || { echo "Missing $conf: SERVER, SERVICE and BOT_DIR (see tools/README.md)" >&2; exit 2; }
# shellcheck source=/dev/null
. "$conf"
: "${SERVER:?not set in $conf}" "${SERVICE:?not set in $conf}" "${BOT_DIR:?not set in $conf}"

files=("nightcheck_$name.txt" "journal_$name.txt")
remote="set -e; ~/nightcheck.sh '$from' '$to' > ~/nightcheck_$name.txt"
remote+="; sudo journalctl -u '$SERVICE' -a --since '$from' --until '$to' > ~/journal_$name.txt"
for kind in probe sampler; do
    [ "${!kind}" = 1 ] || continue
    log=$([ $kind = probe ] && echo goal_probe || echo liiga_sampler)
    remote+="; sudo cp '$BOT_DIR/tools/logs/$log.log' ~/${log}_server.txt; sudo chown \"\$(id -un):\" ~/${log}_server.txt"
    files+=("${log}_server.txt")
done
sources=()
for f in "${files[@]}"; do sources+=("$SERVER:$f"); done

if [ $dry_run = 1 ]; then
    echo "ssh -t <server> \"$remote\""
    echo "scp ${files[*]/#/<server>:} into tools/logs/, renamed to .log"
    exit 0
fi

mkdir -p tools/logs
ssh -t "$SERVER" "$remote"
scp "${sources[@]}" tools/logs/
for f in "${files[@]}"; do
    mv "tools/logs/$f" "tools/logs/${f%.txt}.log"
    echo "  tools/logs/${f%.txt}.log ($(wc -l < "tools/logs/${f%.txt}.log" | tr -d ' ') lines)"
done
echo "Now in Claude Code: /post-game $name"
