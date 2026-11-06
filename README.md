# Rewind

**Restore damaged prices. Keep newer invoices and payment updates.**

Rewind helps you fix selected database values using a good backup. You choose the damaged columns and records, review what will change, and download a checked recovery copy. Every other value comes from today's database.

For example, a bad import multiplies 120 invoice prices by 100. Since the backup, you have added 7 invoices and marked 20 invoices paid. Rewind restores those 120 prices and keeps all 127 invoices and the 20 payment updates.

## Use it

1. Upload the last good SQLite snapshot and today's snapshot, or open the fictional invoice example.
2. Select the damaged columns and records. Rewind shows the values that would be restored.
3. Review the plan, including newer records and other changes that will stay.
4. Approve the recovery. Vultr Serverless Inference writes a recovery attempt, which runs on temporary copies.
5. Download the recovered database after independent checks and sandbox removal pass. The checks can be downloaded too.

The downloaded file is a recovery copy. Connecting it to your application is a separate operator decision. You can inspect snapshots and review a plan before connecting a model; starting the recovery requires configured Vultr Inference.

## Why use a recovery copy

Restoring the whole backup would discard the seven newer invoices and the twenty payment updates in the example. Rewind takes only the values you explicitly selected from the backup and keeps everything else from the current snapshot.

Select only damaged cells. If a selected cell also contains a newer legitimate edit, that edit would be replaced. This version cannot decide which change you intended to keep.

## Vultr and isolated execution

The deployment package runs the controller on a Vultr VM and connects it to Vultr Serverless Inference. The model receives table and column names, declared types, selected column names, and the selected record count. Database files, row values, selected primary-key values, access codes, and credentials stay on the controller and its copies.

All uploaded SQLite inspection runs in fixed trusted gVisor workers. The generated recovery program runs in a separate gVisor container. A fresh trusted container reads the candidate output, and the controller compares it with the original snapshots. The generated program never receives the acceptance checks.

A result must preserve the schema, current primary keys, automatic ID sequences, newer records, and every unselected cell. Every selected replacement must exactly match its backup value. A failed candidate can receive one correction attempt. Provider or network failures stop the recovery.

Sandboxes use the complete checksum-pinned gVisor bundle and digest-pinned Python image. They run without network access, as user 65532, with read-only inputs and root filesystem, 192 MiB RAM, half a CPU, 48 PIDs, an eight-second deadline, bounded output, and no controller credentials. Download requires verified output, confirmed removal of every sandbox, and a fresh artifact fingerprint check.

The controller processes one job at a time, has a four-place queue, allows twelve uploads and four recoveries per day by default, and stores at most 32 workspaces. Workspace reads and downloads require a private capability. A restart fails interrupted jobs instead of silently restarting a model request.

## First-version scope

Use two consistent SQLite snapshots, each up to 256 KiB, with the same ordinary table and schema. The table needs one non-null integer or text primary key, 2–16 ordinary columns, and at most 1,000 rows. Use SQLite's Online Backup API when taking a live database copy; a WAL database file alone is rejected.

Virtual tables, views, triggers, generated columns, blobs, multiple user tables, and oversized values are unsupported. This prototype has no production database connection, automatic retention policy, or workspace deletion screen. Keep test files private and remove stored workspaces as an operator after use.

## Run locally

Python 3.12 or newer, an existing Docker daemon, and the pinned gVisor runtime are required. Runtime setup reloads Docker and verifies that its defaults and existing containers stay intact.

```sh
sudo bash infra/install-gvisor.sh
cp .env.example .env
```

Add your Vultr Inference key and a private demo access code to `.env`. Load that file through your service manager or shell, then run:

```sh
python3 -m rewind.server
```

The default address is `http://127.0.0.1:18182`. Set `REWIND_ORIGIN` to the exact browser origin when using a port forward or public hostname. Keep `.env` private.

## Verify

```sh
python3 -m unittest discover -s tests -v
python3 scripts/check-isolation.py
npm ci --no-audit --no-fund
node --check web/app.js
node scripts/browser-check.mjs
node scripts/browser-check.mjs --plan-only
```

The isolation check uses explicitly labelled reference programs. It verifies selective recovery, rejects whole-backup replacement, probes containment and limits, and confirms teardown. The `--plan-only` browser check inspects the fictional snapshots and verifies the selected recovery plan. These checks make no model calls.

With a configured live deployment, put its private code in an ignored local file and run the actual browser recovery once:

```sh
REWIND_TEST_URL=https://your-rewind-host \
REWIND_ACCESS_FILE=/private/path/to/access-code.txt \
node scripts/browser-check.mjs --live --record
```

This requests a real Vultr recovery. The recorder never starts an automatic second job. The resulting evidence, artifact, screenshots and video stay in ignored `reports/`.

## Deploy beside ReproLab

Build a release with `python3 scripts/build-release.py`. Copy the archive to the Vultr VM, verify its reported SHA-256, and unpack it into a private staging directory. The installer supports an existing ReproLab Caddy service and a new hostname such as `rewind.139.84.249.47.sslip.io`:

```sh
sudo python3 -m rewind.deploy \
  --hostname rewind.139.84.249.47.sslip.io \
  --provider-env /etc/reprolab/reprolab.env \
  --caddy-config /etc/caddy/reprolab.Caddyfile \
  --caddy-service reprolab-https.service
```

Inspect the actual provider environment path and Caddy service on your VM first. The installer preserves the existing proxy configuration and adds only Rewind's route. It creates a separate service, state directory, private reviewer code and sandbox cleanup timer. It reloads the proxy and starts only Rewind. Conflicting files, users, runtimes or routes stop installation.

## Current evidence

Seventeen focused unit tests and actual gVisor integration pass, including selective reference recovery with 120 restored prices, 127 retained invoices, seven newer records, and twenty preserved payment updates. Ten bounded sandbox runs verify recovery, inspection, rejection of whole-backup replacement, containment, time/output limits and confirmed removal. The desktop/mobile interface and actual database-selection plan have passed browser checks. Live Vultr deployment, a genuine model recovery recording, and public demo playback remain pending.

See [THIRD_PARTY.md](THIRD_PARTY.md) for the MIT-licensed ReproLab components used here.
