# SMF SHOP

Railway-ready SMM reseller system with:

- FastAPI web app
- Telegram bot commands
- SQLite database via SQLAlchemy
- Admin panel at `/admin`
- Reseller REST API under `/api/v1`
- Stock management
- API keys and webhooks
- Referral tracking
- BEP20/TRC20 USDT payment verification helpers
- Background jobs for order status, deposits, and referral payouts

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python initialize_db.py
uvicorn main:app --reload
```

Open:

- Admin panel: `http://localhost:8000/admin`
- API docs: `http://localhost:8000/docs`
- Reseller docs: `http://localhost:8000/api/docs/reseller`

Admin panel login:

- Username is always `admin`
- Password comes from `ADMIN_PASSWORD` (default `admin123`)
- Only Telegram user `ADMIN_ID` (or `ADMIN_TG_ID`) can use `/admin`; everyone else gets **No access**
- Set `ADMIN_PANEL_URL` to your full login link (e.g. `https://your-app.up.railway.app/admin/login`)

## Railway deploy

1. Push this repository to GitHub.
2. Connect GitHub repo to Railway.
3. Add environment variables from `.env.example`.
4. Deploy. Railway uses `railway.json` / `Procfile` to run:

```bash
uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
```

## Telegram commands

- `/start`
- `/menu`
- `/products`
- `/wallet`
- `/deposit AMOUNT TX_HASH`
- `/api`
- `/orders`
- `/referral`
- `/support`
- `/admin` (or `/administration`) — panel URL + username/password for `ADMIN_TG_ID` only
- `/adminstats` for the configured admin Telegram ID

## API examples

All protected endpoints require:

```http
Authorization: Bearer YOUR_API_KEY
```

List products:

```http
GET /api/v1/products
```

Create order:

```http
POST /api/v1/orders/create
Content-Type: application/json

{
  "sku": "capcut_pro_1m",
  "quantity": 1,
  "link": "https://example.com/profile",
  "webhook_url": "https://reseller.example/webhook"
}
```

Check status:

```http
GET /api/v1/orders/SMM-ABCDEFGH
```
