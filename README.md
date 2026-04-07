# Ant-Keeper Tasks

Task manifests for the Cruse Control [ant-keeper](https://github.com/Cruse-Control/ant-keeper) installation.

Each subdirectory contains a `manifest.json` that defines a task registered in ant-keeper. These manifests are the source of truth for what runs on the server — they're registered via `POST /api/tasks` and backed up here via ant-keeper's git sync.

## Tasks

| Task | Type | Owner | Description |
|------|------|-------|-------------|
| `cc-history` | daemon | wyler | Claude Code session store (port 7842) |
| `hello-claude` | agent | wyler | Demo/test agent task |
| `imessage-brain-sync` | scheduled | wyler | Syncs iMessages to Discord |
| `spotify-song-logger` | scheduled | wyler | Logs currently playing track |
| `spotify-playlist-sorter` | scheduled | wyler | Sorts playlists daily |
| `skill-sync-wyler` | host | wyler | Syncs shared Claude Code skills for wyler-zahm |
| `skill-sync-flynn` | host | flynn | Syncs shared Claude Code skills for flynn-cruse |

## Adding a task

Use the `/ant-keeper add` or `/ant-keeper onboard` skill in Claude Code, or register directly via the API:

```bash
curl -X POST http://127.0.0.1:7070/api/tasks \
  -H "Authorization: Bearer $ANT_KEEPER_TOKEN" \
  -H "Content-Type: application/json" \
  -d @my-task/manifest.json
```
