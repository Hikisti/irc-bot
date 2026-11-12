#!/bin/sh
# Run by the deploy on the server, in the checkout, after `git reset --hard`: says whether the bot has to be
# restarted. Exit 0 = restart, 1 = no restart; the reason goes to stdout for the deploy log.
#
# $1 is the commit the running bot was started from (recorded by the deploy after a successful restart), or
# empty when unknown. It is compared with HEAD, not with the checkout the server had before this deploy: a
# deploy that updated the code but failed before its restart once left the bot on stale code, and a later
# docs-only push must not skip the restart that one still owes.
#
# Only files the bot never runs are listed as safe; anything else, a file this list does not know included,
# means restart. Renames are split into a delete and an add so a file moved out of src/ still counts.

started_from=$1

if [ -z "$started_from" ] || ! git cat-file -e "$started_from^{commit}" 2>/dev/null; then
    echo "restart: the commit the running bot was started from is not known"
    exit 0
fi

if ! changed=$(git diff --no-renames --name-only "$started_from" HEAD); then
    echo "restart: could not compare with $started_from"
    exit 0
fi

runtime=$(printf '%s\n' "$changed" | grep -v '^$' \
    | grep -Ev '^(README\.md|CLAUDE\.md|\.gitignore|ruff\.toml|pytest\.ini|requirements-dev\.txt)$' \
    | grep -Ev '^(tests|tools|\.github)/')

if [ -n "$runtime" ]; then
    echo "restart: changed since the running bot started: $(printf '%s' "$runtime" | tr '\n' ' ')"
    exit 0
fi

echo "no restart needed: nothing the bot runs changed since it started from $started_from"
exit 1
