# Deployment

Production runs on the Oracle Cloud server (`ssh oracle` = `ubuntu@137.23.45.178`,
aarch64) as an isolated Docker Compose project in `~/moneymate-backend`:

| Service   | What it is                                   | Reachable at                     |
| --------- | -------------------------------------------- | -------------------------------- |
| `db`      | PostgreSQL 16                                | host loopback `127.0.0.1:15432`  |
| `migrate` | one-shot `alembic upgrade head`, then exits  | —                                |
| `api`     | FastAPI behind gunicorn/uvicorn              | host loopback `127.0.0.1:8010`   |

TLS and the public name `https://moneymate.lovee.dpdns.org` come from the
server's existing Caddy (the `loveedpdnsorg-caddy-1` container, host network),
which proxies that SNI name to `localhost:8010`. Nothing of MoneyMate listens on
a public interface.

The mobile app's `preview` and `production` EAS profiles point at
`https://moneymate.lovee.dpdns.org` (`mobile/eas.json`).

> Render and Aiven MySQL are the previous deployment. The Render service is
> suspended; the Aiven database is no longer written to.

---

## Prerequisite: DNS

`moneymate.lovee.dpdns.org` needs an **A record → 137.23.45.178** at the DNS
host for `lovee.dpdns.org` (DigitalPlat: `dns1/dns2.digitalplat.org`). The
domain has no wildcard; every subdomain is its own record. Caddy obtains the
certificate by itself once the name resolves.

```bash
nslookup moneymate.lovee.dpdns.org 1.1.1.1      # must answer 137.23.45.178
```

---

## Updating the server

From the project root on a workstation:

```bash
tar --exclude='__pycache__' -czf moneymate-backend.tgz \
    app alembic alembic.ini scripts Dockerfile docker-compose.yml \
    gunicorn.conf.py requirements.txt .dockerignore .env.production.example README.md
scp moneymate-backend.tgz oracle:/tmp/
ssh oracle 'tar -xzf /tmp/moneymate-backend.tgz -C ~/moneymate-backend && rm /tmp/moneymate-backend.tgz \
            && cd ~/moneymate-backend && docker compose up -d --build'
```

`migrate` runs before the API on every `up`, so a new migration is applied by
the same command - and against exactly the code that is about to serve.

Check it:

```bash
ssh oracle 'cd ~/moneymate-backend && docker compose ps && curl -s localhost:8010/health'
python scripts/smoke_deployed.py https://moneymate.lovee.dpdns.org   # writes a throwaway account
```

## Secrets

`~/moneymate-backend/.env` (mode 600) was generated on the server with
`openssl rand` and has never left it. Template: `.env.production.example`.

- `POSTGRES_PASSWORD` is hex so it is safe inside the `DATABASE_URL` compose
  builds from it.
- `JWT_SECRET` signs every session; rotating it signs everybody out.
- The image contains no secrets (`.dockerignore` keeps env files out).

## Backups

`scripts/backup_postgres.sh` dumps the database to
`~/moneymate-backend/backups/` (mode 700, files 600) and keeps 14 days. A
systemd timer runs it nightly at 20:45 UTC (02:15 IST):

```bash
ssh oracle 'systemctl list-timers moneymate-backup.timer; ls -la ~/moneymate-backend/backups'
```

Restore into an EMPTY database:

```bash
gunzip -c backups/<file>.sql.gz | docker compose exec -T db psql -U moneymate -d moneymate
```

Copy backups off the server now and then - a disk is not a backup of itself.

## Caddy

The route lives in `~/lovee.dpdns.org/caddy.yaml` (root-owned, shared with the
other apps on the server). The MoneyMate change was purely additive - one name in
`tls.certificates.automate`, one SNI route to `localhost:8010` - and the file
before it is kept as `caddy.yaml.bak-moneymate-<timestamp>`. Always validate
before reloading, because one bad file takes every site on the server down:

```bash
docker exec loveedpdnsorg-caddy-1 caddy validate --config /etc/caddy.yaml --adapter yaml
docker exec loveedpdnsorg-caddy-1 caddy reload   --config /etc/caddy.yaml --adapter yaml
```

## Moving data from the old MySQL database

Not used for this cutover (the client started from an empty database), but
kept and tested (`tests/test_data_copy.py`):

```bash
python scripts/migrate_mysql_to_postgres.py --source <mysql url> --source-ssl --target <postgres url>          # dry run
python scripts/migrate_mysql_to_postgres.py --source <mysql url> --source-ssl --target <postgres url> --apply
```

It builds the target at the source's Alembic revision, copies every row with
its id, moves the sequences on, upgrades to head (so the newer back-fills run
over real data) and compares row counts and money totals table by table.

---

## Mobile app

```bash
cd mobile
eas build -p android --profile preview      # installable APK
eas build -p android --profile production   # Play Store bundle
```

`app.json`'s `slug` must stay `cashbook` - it is what the EAS project id is
registered under. `preview` and `production` must be `https`: Android blocks
cleartext HTTP in release builds.
