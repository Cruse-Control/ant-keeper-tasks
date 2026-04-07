# Ant-Keeper Tasks

Task manifests for the [ant-keeper](https://github.com/Cruse-Control/ant-keeper) instance on the Cruse Control server.

Each subdirectory is a task. The `manifest.json` defines how ant-keeper runs it (type, schedule, credentials, etc.). Some tasks include source code; others reference external repos via `source.repo`.

## Installed tasks

| Task | Type | Owner | Description |
|------|------|-------|-------------|
| `cc-history` | daemon | wyler | Multi-user Claude Code session store |
| `hello-claude` | agent | wyler | Test task |
| `imessage-brain-sync` | scheduled | wyler | iMessage sync |
| `spotify-playlist-sorter` | scheduled | wyler | Spotify playlist organizer |
| `spotify-song-logger` | scheduled | wyler | Spotify listening log |

## Adding a task

Use `/ant-keeper add` or `/ant-keeper onboard` in Claude Code, or `POST /api/tasks` directly.

## Relationship to ant-keeper

This repo contains **what runs**. The [ant-keeper](https://github.com/Cruse-Control/ant-keeper) repo contains the **system that runs it**.
