#!/bin/bash
# Fetches one or more game nights' files from the server into tools/logs/ with one SSH login (run on
# the Mac, from anywhere): for each night the nightcheck.sh output and the bot's raw journal for its
# window, and with --probe / --sampler the goal probe's / Liiga sampler's log. Then `/post-game NAME`
# in Claude Code reads them.
#
#   tools/fetch_night.sh NAME "FROM" "TO" [NAME "FROM" "TO" ...] [--probe] [--sampler] [--dry-run]
#   tools/fetch_night.sh 2026-11-19_nhl "2026-11-18 23:30" "2026-11-19 07:30" --probe
#
# NAME labels the files (tools/logs/journal_NAME.log, nightcheck_NAME.log); FROM and TO are the
# journal window in UTC. The server's address, the bot's unit and its checkout come from
# tools/fetch_night.conf, which is gitignored: this repository is public, so nothing about the
# server is written here. The fetched files hold chat and addresses; *.log keeps them out of git.
#
# Without a password where the server allows it: the journal is read without sudo when the login is
# in a group that may read it, and with BOT_USER set in the conf the probe logs are read through a
# sudo rule for exactly that read (DEPLOY.md). Otherwise sudo asks, once per login.
set -euo pipefail
cd "$(dirname "$0")/.."
usage() { sed -n '7,8p' "$0" | sed 's/^# *//' >&2; exit 2; }
probe=0 sampler=0 dry_run=0 windows=()
for arg in "$@"; do
    case $arg in
        --probe) probe=1 ;;
        --sampler) sampler=1 ;;
        --dry-run) dry_run=1 ;;
        --*) usage ;;
        *) windows+=("$arg") ;;
    esac
done
[ ${#windows[@]} -ge 3 ] && [ $((${#windows[@]} % 3)) = 0 ] || usage
# The values go into a command line run on the server, so only these shapes are accepted.
names=()
for ((i = 0; i < ${#windows[@]}; i += 3)); do
    name=${windows[i]} from=${windows[i+1]} to=${windows[i+2]}
    [[ $name =~ ^[A-Za-z0-9_-]+$ ]] || { echo "NAME may hold only letters, digits, - and _ ($name)" >&2; exit 2; }
    for t in "$from" "$to"; do
        [[ $t =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}$ ]] || { echo "Times look like \"2026-11-19 07:30\" ($t)" >&2; exit 2; }
    done
    names+=("$name")
done
conf=tools/fetch_night.conf
[ -f "$conf" ] || { echo "Missing $conf: SERVER, SERVICE and BOT_DIR (see tools/README.md)" >&2; exit 2; }
BOT_USER=""
# shellcheck source=/dev/null
. "$conf"
: "${SERVER:?not set in $conf}" "${SERVICE:?not set in $conf}" "${BOT_DIR:?not set in $conf}"
[ -z "$BOT_USER" ] || [[ $BOT_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo "BOT_USER in $conf is not a user name" >&2; exit 2; }
files=()
# The copies go in one folder on the server that each run empties first, so they never pile up there.
remote="set -e; mkdir -p ~/fetch_night; rm -f ~/fetch_night/*; if id -nG | grep -qwE 'systemd-journal|adm'; then J=journalctl; else J='sudo journalctl'; fi"
for ((i = 0; i < ${#windows[@]}; i += 3)); do
    name=${windows[i]} from=${windows[i+1]} to=${windows[i+2]}
    remote+="; ~/nightcheck.sh '$from' '$to' > ~/fetch_night/nightcheck_$name.txt"
    remote+="; \$J -u '$SERVICE' -a --since '$from' --until '$to' > ~/fetch_night/journal_$name.txt"
    files+=("nightcheck_$name.txt" "journal_$name.txt")
done
for kind in probe sampler; do
    [ "${!kind}" = 1 ] || continue
    log=$([ $kind = probe ] && echo goal_probe || echo liiga_sampler)
    src="$BOT_DIR/tools/logs/$log.log" dst="~/fetch_night/${log}_server.txt"
    copy="sudo cp '$src' $dst; sudo chown \"\$(id -un):\" $dst"
    if [ -n "$BOT_USER" ]; then
        remote+="; sudo -n -u '$BOT_USER' cat '$src' > $dst 2>/dev/null || { $copy; }"
    else
        remote+="; $copy"
    fi
    files+=("${log}_server.txt")
done
sources=()
for f in "${files[@]}"; do sources+=("$SERVER:fetch_night/$f"); done
if [ $dry_run = 1 ]; then
    echo "ssh -t <server> \"$remote\""
    echo "scp ${files[*]/#/<server>:fetch_night/} into tools/logs/, renamed to .log"
    exit 0
fi
mkdir -p tools/logs
ssh -t "$SERVER" "$remote"
scp "${sources[@]}" tools/logs/
for f in "${files[@]}"; do
    mv "tools/logs/$f" "tools/logs/${f%.txt}.log"
    echo "  tools/logs/${f%.txt}.log ($(wc -l < "tools/logs/${f%.txt}.log" | tr -d ' ') lines)"
done
for name in "${names[@]}"; do echo "Now in Claude Code: /post-game $name"; done
