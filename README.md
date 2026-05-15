# LocalDNS

A self-hosted local DNS server with a web management interface. Define custom records for any TLD, and unknown queries fall back to Cloudflare (1.1.1.1) and Google (8.8.8.8).

## Features

- Custom DNS records — any TLD (`.home`, `.lan`, `.corp`, etc.)
- Record types: A, AAAA, CNAME, MX, TXT, PTR
- Automatic failover to 1.1.1.1 → 8.8.8.8 for unknown names
- Hot-reload — changes in the web UI are reflected in seconds, no restart needed
- Secure web UI — login, CSRF protection, hashed passwords, brute-force guard
- Runs as two Docker containers sharing a SQLite database
- Auto-starts on boot via a systemd service

## Requirements

- Docker + Docker Compose v2
- Port 53 available (see note below)

## Deploy

```bash
git clone <repo-url> localDNS
cd localDNS
./deploy.sh
```

The script will:
1. Check Docker and port 53 availability
2. Generate a random `SECRET_KEY` in `.env`
3. Prompt for the admin username and password
4. Build images and start containers
5. Install and enable a systemd service for auto-start on boot

> **Ubuntu / Debian note:** systemd-resolved may occupy port 53. The script detects this and prints the exact commands to free it before retrying.

## Usage

### Web UI

Open `http://127.0.0.1:5380` (or the server's IP if you changed `WEBUI_HOST`).

Log in with the credentials you set during `deploy.sh`. From there you can add, edit, enable/disable, and delete DNS records.

### Using the DNS server

Point your resolver at the machine running LocalDNS:

| Context | Setting |
|---|---|
| Linux (this machine) | Add `nameserver 127.0.0.1` to `/etc/resolv.conf` |
| Router / DHCP | Set primary DNS to the server's LAN IP |
| Windows client | Preferred DNS → server's LAN IP |

Test it:

```bash
# Local record (after adding one via the UI)
dig @127.0.0.1 myserver.home A

# Upstream failover
dig @127.0.0.1 google.com A
```

## Configuration

Edit `.env` (created automatically on first run):

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | auto-generated | Flask session secret — do not change after first run |
| `WEBUI_HOST` | `127.0.0.1` | Set to `0.0.0.0` to expose the UI on the LAN |
| `WEBUI_PORT` | `5380` | Web UI port |

## Architecture

```
┌─────────────────────┐     ┌─────────────────────┐
│   dns container     │     │   webui container   │
│   port 53 UDP/TCP   │     │   port 5380         │
│                     │     │                     │
│   dnslib server     │     │   Flask + gunicorn  │
│   in-memory cache   │     │   CRUD records      │
│   ↕ watchdog        │     │   auth + CSRF       │
└────────┬────────────┘     └──────────┬──────────┘
         │                             │
         └──────────┬──────────────────┘
                    │
              ./data/dns.db
              (SQLite WAL, shared volume)
```

The DNS server watches the database file's modification time. When the web UI saves a change, the cache reloads within ~1 second — no container restart needed.

## Useful commands

```bash
docker compose logs -f            # Follow logs from both containers
docker compose logs -f dns        # DNS server logs only
docker compose restart dns        # Restart DNS without touching the UI
docker compose ps                 # Check container status
docker compose down               # Stop everything
```

## Security notes

- The web UI is bound to `127.0.0.1` by default — not reachable from the network unless you change `WEBUI_HOST`.
- Passwords are stored as PBKDF2-SHA256 hashes.
- All forms are protected by CSRF tokens (Flask-WTF).
- Login is rate-limited to 5 attempts per IP per 5 minutes.
- The admin password is never passed as a CLI argument or environment variable during setup — it is piped via stdin.

## License

MIT
