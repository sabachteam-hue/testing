# Database Persistence & Backup Guide

This document details the database architecture, persistence model, and disaster recovery procedures for **SMF SHOP** on Railway.

---

## 1. Database Architecture

* **Primary Authoritative Database:** **PostgreSQL** via SQLAlchemy (`postgresql+psycopg://`).
  * All users, stock accounts, orders, transactions, payment methods, bot configurations, and audit logs live exclusively in PostgreSQL.
  * In production (`DEPLOYMENT_ENV=production`), PostgreSQL is mandatory. Startup will fail with a clear error if `DATABASE_URL` is missing or invalid. Silent fallback to SQLite in production is strictly disabled to prevent data fragmentation and data loss.
* **Local Development Fallback:**
  * When `DATABASE_URL` is completely omitted in local development (`DEPLOYMENT_ENV=development`), the application falls back to a safe local SQLite file (`sqlite:///./smm_reseller.db`).

---

## 2. Zero Data Loss & Migration Safety

The database layer follows strict zero-data-loss guidelines:
1. **Never Drop or Truncate:** Tables, schemas, and columns are never dropped or truncated.
2. **Additive & Idempotent Migrations:** All schema updates run inside `run_light_migrations()` using database introspection (`sqlalchemy.inspect`).
   * New columns and indexes are only added if they do not already exist.
   * Running migrations repeatedly is a safe no-op.
3. **Legacy SQLite Preservation:** If an older SQLite database file exists on disk (e.g. from an earlier prototype), it is detected and preserved untouched. It is never overwritten or deleted.

---

## 3. Persistent File Storage (Railway Web-Data Volume)

* Relational records live in PostgreSQL, not in the volume.
* The Railway persistent volume (`RAILWAY_VOLUME_MOUNT_PATH`, e.g., `/app/data` or `/data`) is used exclusively for persistent user-uploaded and admin-uploaded files:
  ```
  $RAILWAY_VOLUME_MOUNT_PATH/
    uploads/
      services/         (product logos and icons)
      categories/       (category logos and icons)
      payment_methods/  (payment gateway icons)
      announcements/    (broadcast announcement images)
      custom_emoji/     (custom emoji raster caches)
  ```
* **Persistence Across Deployments:** Files saved to this volume survive GitHub pushes, container restarts, and code deployments.
* **Dual-Layer Serving:** Static file serving inspects the persistent volume first, and seamlessly falls back to repository-bundled assets for older files.

---

## 4. How to Take a PostgreSQL Backup

### Option A: Railway Dashboard Backups (Recommended)
1. Open your project in the [Railway Dashboard](https://railway.app).
2. Click on your **PostgreSQL** database service.
3. Navigate to the **Backups** tab.
4. Click **Create Backup** (Railway also takes scheduled automated backups).

### Option B: Command-Line Backup (`pg_dump`)
Using your PostgreSQL connection string from Railway:
```bash
# Export compressed custom-format binary backup
pg_dump "$DATABASE_URL" -F c -b -v -f "smf_shop_backup_$(date +%Y%m%d_%H%M%S).dump"

# Or export plain-text SQL script
pg_dump "$DATABASE_URL" -F p -v -f "smf_shop_backup_$(date +%Y%m%d_%H%M%S).sql"
```

### Option C: Read-Only Application JSON Export
You can take a self-contained snapshot of all application models directly using the built-in CLI export tool without needing external PostgreSQL client binaries:
```bash
# Run inside container or local environment with DATABASE_URL set
python -m utils.db_export --output "smf_backup_$(date +%Y%m%d).json"
```

---

## 5. How to Restore a PostgreSQL Backup

> [!CAUTION]
> Always create a backup of your existing database before performing any restore operation.

To restore a custom-format dump into your PostgreSQL instance:
```bash
pg_restore -d "$DATABASE_URL" -v --clean --if-exists "smf_shop_backup_YYYYMMDD.dump"
```

---

## 6. Recommended Backup Schedule

| Asset | Tool / Mechanism | Recommended Frequency |
| :--- | :--- | :--- |
| **PostgreSQL Database** | Railway Automated Backups | Daily |
| **Pre-Deployment Snapshot** | `python -m utils.db_export` or `pg_dump` | Before major schema updates |
| **Uploaded Media Files** | Volume snapshot / SFTP | Weekly |
