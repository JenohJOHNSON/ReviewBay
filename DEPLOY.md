# Deploying ReviewBay 24/7 (Oracle Cloud Always Free)

This runs the whole app (API plus the scraper worker) around the clock on a free
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
scp -r -i ~/Downloads/ssh-key-*.key "/Users/johnymohamedgaousejakkirhussain/Downloads/Claude AD/PROJECT JEN" ubuntu@YOUR_PUBLIC_IP:~/reviewbay
```

That copies everything into `~/reviewbay` on the server.

Alternative (from GitHub instead of copying): on the server, install GitHub CLI,
run `gh auth login`, then `gh repo clone javidjmg28/reviewbay ~/reviewbay`, and
create `.env` from `.env.example` with your secrets.

## 6. Set a password (do this for a public server)

On the SERVER:

```
cd ~/reviewbay
nano .env
```

Find these two lines near the bottom and set a login you choose:

```
AUTH_USER=youruser
AUTH_PASS=pick-a-long-password
```

Save with Ctrl then O then Enter, exit with Ctrl then X.

## 7. Start it

On the server:

```
cd ~/reviewbay
docker compose up -d --build
```

The first build takes a few minutes. The first time you use the chat, the API
downloads a roughly 400 MB model, so that one is slow, then fast after. Check it:

```
curl -s localhost:8000/healthz
```

You should see `{"status":"ok"}`.

## 8. Open it in your browser

```
http://YOUR_PUBLIC_IP:8000
```

It asks for the username and password from step 6. That is ReviewBay, live 24/7,
running whether or not your laptop is on.

## Running it day to day

- Both services use `restart: unless-stopped`, so they come back after a reboot.
- Logs: `docker compose logs -f api` or `docker compose logs -f ingestion`.
- Update after code changes: `git pull` (if you cloned) then `docker compose up -d --build`.
- Stop: `docker compose down`. Start: `docker compose up -d`.

## Later upgrades (optional)

- A clean web address with HTTPS instead of `http://IP:8000`: run a Cloudflare
  Tunnel on the server, or put Caddy in front with a domain (automatic HTTPS).
- Note: `http://IP:8000` with a password protects access, but plain http is not
  encrypted in transit. Use a throwaway password until HTTPS is added.
