# Deploying ReviewBay 24/7 (Oracle Cloud Always Free)

This runs the whole app (API plus the cloud worker) around the clock on a free
Linux server, so it stays live even when your laptop is off. It is the same
`docker compose` you run locally, just on an always-on machine.

You need: a card (Oracle uses it only to verify you; Always Free resources do not
charge) and about 30 minutes.

Replace `YOUR_PUBLIC_IP` and the key path everywhere below with your own.

## 1. Create the free server

1. Sign up at https://www.oracle.com/cloud/free/ and finish verification.
2. Console menu, Compute, Instances, Create instance. Name it `reviewbay`.
3. "Image and shape", Edit:
   - Image: Canonical Ubuntu 22.04.
   - Shape: Ampere, `VM.Standard.A1.Flex`, set 2 OCPU and 12 GB memory (inside the
     Always Free allowance; 1 OCPU / 6 GB also works).
   - If you see "out of capacity", try another Availability Domain, or try again
     later. ARM capacity comes and goes.
4. Networking: keep the default VCN and subnet, and make sure "Assign a public
   IPv4 address" is Yes.
5. "Add SSH keys": choose "Generate a key pair for me" and download BOTH keys.
   Keep the private one safe, you log in with it.
6. Create. Wait about a minute, then copy the instance's Public IP address.

## 2. Open the app port in the cloud firewall

Oracle blocks everything except SSH by default, in two layers. First layer:

- Networking, Virtual Cloud Networks, your VCN, Security Lists, "Default Security List".
- Add Ingress Rule: Source CIDR `0.0.0.0/0`, IP Protocol TCP, Destination Port `8000`. Save.

The second layer (the server's own firewall) is handled in step 4.

## 3. Log in to the server

On your Mac, open Terminal and run (fix the key name and IP):

```
chmod 400 ~/Downloads/ssh-key-*.key
ssh -i ~/Downloads/ssh-key-*.key ubuntu@YOUR_PUBLIC_IP
```

Type `yes` if it asks to trust the host. You are now on the server.

## 4. Open the server firewall and install Docker

Run this whole block on the server:

```
sudo iptables -I INPUT -p tcp --dport 8000 -j ACCEPT
sudo apt-get update -y && sudo apt-get install -y netfilter-persistent iptables-persistent
sudo netfilter-persistent save
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
```

Then `exit` and SSH back in (step 3) so the docker group takes effect.

## 5. Copy the app to the server

Easiest path: copy your local folder, which already has your filled-in `.env`.
Run this on your MAC (a new Terminal tab, not the server):

```
scp -r -i ~/Downloads/ssh-key-*.key "/path/to/ReviewBay" ubuntu@YOUR_PUBLIC_IP:~/reviewbay
```

That copies everything into `~/reviewbay` on the server.

Alternative (from GitHub instead of copying): on the server, install GitHub CLI,
run `gh auth login`, then `gh repo clone javidjmg28/reviewbay ~/reviewbay`, and
create `.env` from `.env.example` with your secrets.

## 6. Decide who can sign up (do this for a public server)

ReviewBay has accounts: every page except the landing page needs one, and each
person's brands and saved reports belong to their account. Signup is OPEN by
default, and every brand someone adds is scraped on a schedule, which costs
money. On the SERVER:

```
cd ~/reviewbay
nano .env
```

Set these near the bottom:

```
ADMIN_EMAILS=you@example.com          # your own address; create this account first
SIGNUP_INVITE_CODE=pick-a-code        # anyone signing up must type it
```

Once everyone has an account you can close registration with
`SIGNUP_ENABLED=0`. The old site-wide password (`AUTH_USER`/`AUTH_PASS`) still
works as an extra wall in front of everything, but it is no longer what keeps
one person's brands and reports from another.

Save with Ctrl then O then Enter, exit with Ctrl then X.

## 7. Start it

On the server:

```
cd ~/reviewbay
./scripts/test.sh
docker compose up -d --build
```

Then apply the database schema. **This is required before anyone can sign
in**: the accounts live in tables that migration 003 creates. It is safe to
re-run; every migration in `postgres/` is additive and idempotent, and the
runner refuses to execute anything that drops, truncates or retypes:

```
docker compose run --rm worker python scripts/migrate.py --dry-run   # look first
docker compose run --rm worker python scripts/migrate.py             # apply
```

The first build takes a few minutes. Enrichment uses the OpenAI embeddings API,
so there is no local model download. Check the website:

```
curl -s localhost:8000/healthz
curl -s localhost:8000/readyz
```

`/healthz` should return `{"status":"ok"}`. `/readyz` should also return
`"status":"ok"` once `DATABASE_URL` is set correctly and Neon is reachable.

## 8. Open it in your browser

```
http://YOUR_PUBLIC_IP:8000
```

Choose "Add your first brand" and create your account with the address you put
in `ADMIN_EMAILS` (and the invite code, if you set one). That is ReviewBay, live
24/7, running whether or not your laptop is on.

## Running it day to day

- Both services use `restart: unless-stopped`, so they come back after a reboot.
- Logs: `docker compose logs -f api` or `docker compose logs -f worker`.
- Update after code changes: `git pull` (if you cloned) then `docker compose up -d --build`.
- Stop: `docker compose down`. Start: `docker compose up -d`.

## Deploying on Railway instead

`docker-compose.yml` runs **two** services, and both matter:

| Service | What it does | Public? |
|---|---|---|
| `api` | the website, the chatbot, and onboarding-time collection | yes |
| `worker` | the Airbyte import, the free local sources, enrichment, and the QC pass | no |

A compose deployment gets both. A Railway deployment needs them created as two
services, because `railway.json` describes only the API — so a Railway project
built from that file alone has no worker, and scheduled refresh, the Airbyte
import and the global QC pass never run. The symptom is subtle rather than loud:
the site works, the dashboard shows data, and the data quietly stops being
refreshed.

To run both on Railway, create two services from the same repository and point
each at its own config file (Railway service settings → *Config-as-code path*):

| Service | Config-as-code path | Networking |
|---|---|---|
| `reviewbay-api` | `railway.json` | generate a domain |
| `reviewbay-worker` | `railway.worker.json` | none — it serves no HTTP |

Give both the same environment variables; the worker needs `DATABASE_URL` and
`OPENAI_API_KEY` (or `OPENROUTER_API_KEY`) at minimum. The worker refreshes
every brand any account tracks, read from the database, so it no longer needs
to share a disk with the API. Do **not** set a healthcheck path on the worker: it
is a loop, not a server, and Railway would fail the deploy waiting for an HTTP
200 that never comes.

If you would rather run one service, that is a legitimate choice for a small
deployment — but then nothing runs the worker's cycle, so schedule it externally
instead of leaving the gap:

```
RUN_ONCE=1 python -m reviewbot.worker
```

That performs exactly one cycle and exits, which is what a Railway cron service
or any external scheduler wants. `WORKER_INTERVAL_SECONDS` (default 900) only
applies to the long-running form.

The browser tier for client-rendered review pages belongs on the worker and
nowhere else:

```
docker build --build-arg WITH_BROWSER=1 -f docker/worker.Dockerfile .
```

It adds roughly 400MB and a Chromium process of 150-300MB resident. The API
serves a dashboard on a small instance and has no business launching browsers.

## The database on Railway

ReviewBay needs Postgres with the **pgvector** extension. Railway's standard
PostgreSQL template does not include extensions (Railway's docs say they will
not add any), so the database is Railway's documented pgvector template, which
runs the pgvector project's own image (`pgvector/pgvector:pg18`) on a persistent
volume. Railway generates its password; nobody has to handle it.

Wire it to the app with a reference, so the password never leaves Railway and
traffic stays on the private network:

```
DATABASE_URL=${{pgvector.DATABASE_URL_PRIVATE}}
```

Migrations then need no manual step: `railway.json` sets
`preDeployCommand = python scripts/migrate.py`, so every deploy migrates the
database before the new version starts. If a migration fails, the deploy stops
and the previous version keeps serving.

### Moving existing data in (done once)

`railway.dbsetup.json` describes a one-off job: it migrates the new database,
then runs `scripts/copy_database.py` to copy every ReviewBay table from the old
one. Create a temporary service from this repository with that config file and
two variables, both references, so again no password is typed anywhere:

```
DATABASE_URL=${{pgvector.DATABASE_URL_PRIVATE}}     # copy into
SOURCE_DATABASE_URL=${{ReviewBay.DATABASE_URL}}      # copy from (the old database)
```

The copy never writes to the source and never overwrites a row in the target,
so running it again only fills in rows written to the old database during the
switch. Delete the temporary service afterwards.

### Rolling back

The old database is left exactly as it was. Pointing `DATABASE_URL` back at it
(from its provider's console) restores the previous state; anything written to
the new database in the meantime stays there.

## Upgrading a deployment that predates accounts

1. **Migrate first.** From your own machine, with the production connection
   string (the runner prints only the host, never the password):

   ```
   DATABASE_URL="postgresql://...neon.tech/...?sslmode=require" python scripts/migrate.py --dry-run
   DATABASE_URL="postgresql://...neon.tech/...?sslmode=require" python scripts/migrate.py
   ```

   Both images also contain the runner now, so `python scripts/migrate.py` works
   from a Railway service shell or `docker compose run --rm api ...` too. If the
   new code goes out before the migration, nothing breaks silently: sign-up
   answers with the command to run.

2. **Set `ADMIN_EMAILS`** and a signup policy (step 6 above), then deploy.

3. **Create your account.** Brands that were in your browser before accounts are
   still in its storage; the My brands page offers to import them. Each person
   gets the same offer in their own browser.

4. **Saved reports from before accounts** have no owner. They stay reachable
   through their share links, and admins can still open them.

## Later upgrades (optional)

- A clean web address with HTTPS instead of `http://IP:8000`: run a Cloudflare
  Tunnel on the server, or put Caddy in front with a domain (automatic HTTPS).
- Note: `http://IP:8000` with a password protects access, but plain http is not
  encrypted in transit. Use a throwaway password until HTTPS is added.
