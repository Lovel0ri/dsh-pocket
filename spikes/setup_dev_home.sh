#!/bin/sh
# setup_dev_home.sh — build a self-contained, writable DSH_HOME for ACP spikes.
#
# The live desktop app owns ~/.dsh (its active sessions hold the per-session
# flock), and this agent's sandbox may not write there anyway. Everything the
# spikes need is therefore copied once into 04_tools/dsh-pocket/.dev-home:
#
#   * credentials          — read-only copy of ~/.dsh/.credentials.yaml
#   * workspace registry   — ~/.dsh/storages/workspace.json (ids/archive/pin)
#   * session logs         — a fixed, small set of real session directories
#   * profile              — the shipped `acp` profile, materialized on first
#                            `dsh --profile acp --dump-config` run
#
# Idempotent: re-running refreshes the session copies and re-checks the rest.
# Never writes to ~/.dsh. Safe to delete .dev-home and re-run at any time.
#
# Usage: sh spikes/setup_dev_home.sh [--with-provider]
#   --with-provider  additionally materialize the `pocket` profile: the shipped
#                    acp profile plus the dsh-our-free-model bundle, by linking
#                    the plugin already installed in the desktop profile. Used
#                    for the "restore a non-DeepSeek session over ACP" spike.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$HERE/.." && pwd)
DEV_HOME="$ROOT/.dev-home"
LIVE_HOME="${LIVE_DSH_HOME:-$HOME/.dsh}"
CLI="/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
WORKSPACE="/Volumes/extra1tb/clawdbot-project"
PROJECT_KEY="--Volumes-extra1tb-clawdbot-project--"

# Root sessions deliberately chosen to cover every wire shape the client needs:
#   session-1e813b14…  latest route = our-free-model      (resume must fail without the plugin)
#   session-3a77d3aa…  latest route = deepseek-official   (resume + prompt path, has tool calls)
#   session-5f856f7f…  latest route = our-free-model      (second provider case)
#   cad8b179-…         origin=subagent, bare-UUID dir      (must be invisible to session/list)
SESSIONS="session-1e813b14-f293-480d-9e0e-4b047212d4eb \
session-3a77d3aa-2332-4758-9408-ada04dd21ed7 \
session-5f856f7f-f89f-488d-a81b-00c5948dc9a9 \
cad8b179-20a5-4c2d-862e-585a714614c2"

log() { printf '%s\n' "== $*"; }

log "dev home: $DEV_HOME"
mkdir -p "$DEV_HOME/sessions/$PROJECT_KEY" "$DEV_HOME/storages" "$DEV_HOME/profiles"

# ---------------------------------------------------------------- credentials
if [ -f "$LIVE_HOME/.credentials.yaml" ] && [ ! -f "$DEV_HOME/.credentials.yaml" ]; then
  install -m 600 "$LIVE_HOME/.credentials.yaml" "$DEV_HOME/.credentials.yaml"
  log "copied credentials (mode 600)"
else
  log "credentials: $( [ -f "$DEV_HOME/.credentials.yaml" ] && echo 'already present' || echo 'NOT FOUND — real model calls will be skipped' )"
fi

# ------------------------------------------------------------ workspace index
if [ -f "$LIVE_HOME/storages/workspace.json" ]; then
  cp "$LIVE_HOME/storages/workspace.json" "$DEV_HOME/storages/workspace.json"
  log "copied workspace registry"
fi

# ---------------------------------------------------------------- session logs
for id in $SESSIONS; do
  src="$LIVE_HOME/sessions/$PROJECT_KEY/$id"
  dst="$DEV_HOME/sessions/$PROJECT_KEY/$id"
  if [ -d "$src" ]; then
    rm -rf "$dst"
    cp -R "$src" "$dst"
    # A copied directory is a fresh inode: the live writer's flock does NOT
    # carry over, so spikes can write these logs without touching the desktop.
    printf '%s\n' "   session $id ($(ls "$dst" | tr '\n' ' '))"
  else
    log "WARN session $id not found in $src"
  fi
done

# --------------------------------------------------------------- profile (acp)
# --dump-config materializes $DSH_HOME/profiles/acp from the shipped template
# without booting the profile or opening a socket.
if [ ! -f "$DEV_HOME/profiles/acp/package.json" ]; then
  DSH_HOME="$DEV_HOME" "$CLI" --profile acp --dump-config >"$DEV_HOME/profiles-acp.dump.yml"
  log "materialized shipped acp profile"
fi

# ------------------------------------------------- pocket profile (optional)
if [ "${1:-}" = "--with-provider" ]; then
  POCKET="$DEV_HOME/profiles/pocket"
  mkdir -p "$POCKET/cordis.patch.yml.d" 2>/dev/null || true
  cp "$DEV_HOME/profiles/acp/cordis.yml" "$POCKET/cordis.yml"
  cp "$DEV_HOME/profiles/acp/pnpm-workspace.yaml" "$POCKET/pnpm-workspace.yaml"
  cat >"$POCKET/package.json" <<'JSON'
{
  "name": "dsh-profile-pocket",
  "private": true,
  "dependencies": {},
  "dsh": {
    "profile": {
      "bundles": [
        "@deepseek-ai/dsh-base",
        "@deepseek-ai/dsh-acp-app",
        "dsh-our-free-model"
      ]
    }
  }
}
JSON
  cat >"$POCKET/cordis.patch.yml" <<'YAML'
# dsh-pocket spike profile: shipped acp tree + the our-free-model bundle.
[]
YAML
  # The bundle is already installed for the desktop profile; link it instead of
  # downloading. A production install would run: dsh plugin --profile pocket add
  # file:~/.dsh/profiles/desktop/vendor/dsh-our-free-model-2.5.9.tgz
  mkdir -p "$POCKET/node_modules"
  if [ -d "$LIVE_HOME/profiles/desktop/node_modules/dsh-our-free-model" ] && [ ! -e "$POCKET/node_modules/dsh-our-free-model" ]; then
    ln -s "$LIVE_HOME/profiles/desktop/node_modules/dsh-our-free-model" "$POCKET/node_modules/dsh-our-free-model"
    log "pocket profile: linked dsh-our-free-model from the desktop profile"
  fi
  log "pocket profile ready: $POCKET"
fi

log "done. run spikes with: DSH_HOME=$DEV_HOME python3 spikes/acp_probe.py --help"
