# Deployment

Backend runs as two containers (MySQL + the API behind gunicorn). The mobile
app is built by EAS.

---

## Backend

### First deploy

```bash
cp .env.production.example .env.production
# Fill in every CHANGE_ME. Generate the JWT secret with:
python -c "import secrets; print(secrets.token_urlsafe(48))"

docker compose --env-file .env.production up -d --build
```

Order is handled by compose: `db` starts and must pass its healthcheck, then
`migrate` creates the tables and exits, then `api` starts.

```bash
docker compose --env-file .env.production ps
docker compose --env-file .env.production logs -f api
curl -s http://127.0.0.1:8000/health     # {"status":"ok"}
```

### Updating

```bash
git pull
docker compose --env-file .env.production up -d --build
```

`migrate` re-runs on each `up`. That is safe — `create_all` only creates
missing tables — but see the migrations note below.

### Migrations

`scripts/create_tables.py` creates tables that do not exist. **It never alters
one that does.** Adding a column to a model will not change the live database;
the change has to be applied by hand:

```bash
docker compose --env-file .env.production exec db \
  mysql -u root -p"$MYSQL_ROOT_PASSWORD" cash_book \
  -e "ALTER TABLE transactions ADD COLUMN client_ref VARCHAR(64) NULL UNIQUE;"
```

This is the point to adopt Alembic. Once it is in, swap the `migrate` service's
command to `alembic upgrade head` and this problem goes away.

### Backups

The database lives in the `cashbook_db_data` volume. Nothing else is stateful.

```bash
# Dump
docker compose --env-file .env.production exec -T db \
  mysqldump -u root -p"$MYSQL_ROOT_PASSWORD" --single-transaction cash_book \
  > backup-$(date +%F).sql

# Restore
docker compose --env-file .env.production exec -T db \
  mysql -u root -p"$MYSQL_ROOT_PASSWORD" cash_book < backup-2026-09-03.sql
```

### Before this faces the internet

- **Terminate TLS in front of it.** The API binds to `127.0.0.1:8000`, so it is
  not reachable from outside the host as configured. Put nginx/Caddy or a load
  balancer in front. The app stores a JWT — sending it over plain HTTP hands it
  to anyone on the path.
- **Set `CORS_ORIGINS`** to the real origins. `*` is the development default.
- **Do not publish the `db` port.** It is commented out in
  `docker-compose.yml`; leave it that way unless you specifically need a GUI
  client, and bind it to `127.0.0.1` if you do.
- **`JWT_SECRET` is per environment.** Rotating it logs every user out.

### Secret handling

- The image contains no secrets: `.dockerignore` keeps env files out of the
  build context and the Dockerfile copies none.
- Values are injected at run time by compose from `--env-file`.
- `.env` (development) and `.env.production` are both gitignored; the
  `.example` files are the templates and are committed.
- Compose fails loudly rather than defaulting when `MYSQL_ROOT_PASSWORD`,
  `DB_USER`, `DB_PASSWORD` or `JWT_SECRET` is missing.
- MySQL's root password and the app's `DB_PASSWORD` are separate. The app
  account should have rights to `cash_book` only.

---

## Mobile app

### One-time setup

```bash
npm install -g eas-cli
eas login
cd mobile
eas init          # creates the EAS project and writes extra.eas.projectId
```

`eas init` is required before any build — it links this app to your Expo
account. It is the one step that cannot be done ahead of time here.

### Point the build at your API

`eas.json` carries `EXPO_PUBLIC_API_URL` per profile, and the placeholder
values must be replaced:

| Profile | Currently | Set it to |
| --- | --- | --- |
| `development` | `http://192.168.29.10:8000` | your machine's LAN IP |
| `preview` | `https://staging-api.example.com` | your staging URL |
| `production` | `https://api.example.com` | your production URL |

This matters more than it looks. `src/api/client.ts` falls back to a LAN IP
when nothing else is set, and in a release build there is no Metro host to
infer one from — so an unset value ships an app that points at a private
address and simply cannot connect.

**Preview and production must be `https`.** Android blocks cleartext HTTP in
release builds (verified against the generated manifest: cleartext is enabled
only in the debug variant). A `preview` APK pointed at `http://` will fail
every request with no obvious explanation.

### Android builds

```bash
cd mobile

# Installable APK for a physical device — this is the one for testing.
eas build -p android --profile preview

# Play Store bundle (.aab)
eas build -p android --profile production

# Dev client for day-to-day work against a local backend
eas build -p android --profile development
```

The build runs on EAS servers; the CLI prints a URL to follow it, and a
download link when it finishes. First run asks to generate an Android keystore
— say yes and let EAS hold it, unless you already have one.

Install the APK on a device:

```bash
# From the download link, or straight over adb:
eas build:run -p android --latest
# or
adb install ~/Downloads/build-xxxx.apk
```

An `.aab` cannot be installed directly on a phone — it is for the Play Store.
Use the `preview` profile for anything you want to sideload.

### Useful follow-ups

```bash
eas build:list                       # history and status
eas build:view                       # detail for one build
eas submit -p android --latest       # upload the .aab to Play (internal track)
eas build -p android --profile preview --local   # build on this machine
```

### Version numbers

`cli.appVersionSource` is `remote`, so EAS owns the build number and
`production` has `autoIncrement` on — `versionCode` goes up on its own. The
user-facing `version` in `app.json` ("1.0.0") is yours to bump.

### iOS

`ios.bundleIdentifier` is set, so `eas build -p ios` will work, but it needs a
paid Apple Developer account. Nothing here has been exercised against one.
