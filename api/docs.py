from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from utils.helpers import get_public_base_url

router = APIRouter(tags=["docs"])


@router.get("/api/docs/reseller", response_class=HTMLResponse)
def reseller_docs() -> str:
    base = get_public_base_url() or "https://your-app.up.railway.app"
    api = f"{base}/api/v1"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Reseller API Docs · SMF SHOP</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {{ --ink:#151a23; --muted:#6b7380; --fill:#e8eef5; --navy:#152033; --line:#e2e7ef; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family:"Plus Jakarta Sans",sans-serif; background:#eef1f6; color:var(--ink); line-height:1.55; }}
    .wrap {{ max-width:760px; margin:40px auto; padding:0 18px 60px; }}
    .card {{ background:#fff; border:1px solid var(--line); border-radius:18px; padding:28px 28px 32px; box-shadow:0 10px 30px rgba(21,32,51,.08); }}
    h1 {{ margin:0 0 8px; font-size:1.8rem; letter-spacing:-.03em; }}
    .sub {{ color:var(--muted); margin:0 0 24px; }}
    h2 {{ font-size:1.05rem; margin:28px 0 10px; }}
    code, pre {{ background:var(--fill); border-radius:10px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.9rem; }}
    code {{ padding:2px 7px; }}
    pre {{ padding:14px 16px; overflow:auto; }}
    ul {{ padding-left:1.2rem; color:var(--muted); }}
    li {{ margin:6px 0; }}
    .pill {{ display:inline-block; background:var(--navy); color:#fff; border-radius:999px; padding:6px 12px; font-size:.8rem; font-weight:600; }}
    a {{ color:var(--navy); }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <span class="pill">Reseller API</span>
      <h1>API Documentation</h1>
      <p class="sub">Connect your bot, website, or panel. Orders use live stock and debit the wallet balance.</p>

      <h2>Authentication</h2>
      <p>Send your API key with every request:</p>
      <pre>Authorization: Bearer YOUR_KEY</pre>

      <h2>Base URL</h2>
      <pre>{api}</pre>

      <h2>Endpoints</h2>
      <ul>
        <li><code>GET {api}/products</code> — list products</li>
        <li><code>GET {api}/products/{{sku}}</code> — product detail</li>
        <li><code>GET {api}/account/balance</code> — wallet balance</li>
        <li><code>POST {api}/orders/create</code> — place order</li>
        <li><code>GET {api}/orders/{{order_code}}</code> — order status</li>
        <li><code>GET {api}/stats</code> — usage / rate-limit info</li>
        <li><code>POST {api}/webhooks/register</code> — register webhooks</li>
        <li><code>GET {api}/webhooks</code> — list webhooks</li>
      </ul>

      <h2>Place order body</h2>
      <pre>{{
  "sku": "capcut_pro_1m",
  "quantity": 1,
  "link": "https://example.com/profile",
  "webhook_url": "https://your.site/hook"
}}</pre>

      <h2>Notes</h2>
      <ul>
        <li>Keys are created automatically when a client opens <code>/api</code> in the Telegram bot.</li>
        <li>Clients can generate a new key or revoke anytime from the API panel buttons.</li>
        <li>Admin can also issue keys with a custom per-hour rate limit from Admin → API Management.</li>
        <li>Wallet is charged when the order is placed.</li>
      </ul>
    </div>
  </div>
</body>
</html>
"""
