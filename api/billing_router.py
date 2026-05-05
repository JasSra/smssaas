"""
Billing endpoints:
  POST /admin/tenants/provision  — create tenant + Stripe customer + subscription
  POST /billing/webhook          — Stripe event receiver
  POST /billing/sync             — manual: push this month's usage to Stripe
  GET  /billing/portal           — return Stripe billing portal URL for tenant
"""
import os
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
from datetime import datetime, timezone

import billing
from db import db

router = APIRouter(tags=["billing"])


# ── Admin: provision new paying tenant ───────────────────────────────────────

class TenantProvision(BaseModel):
    email: str
    name: str
    plan: str = "starter"
    webhook_url: str | None = None


@router.post("/admin/tenants/provision")
def provision_tenant(body: TenantProvision, x_admin_key: str = Header(...)):
    import secrets, uuid
    if x_admin_key != os.environ.get("ADMIN_KEY", "changeme"):
        raise HTTPException(status_code=401)

    tenant_id = str(uuid.uuid4())
    api_key = secrets.token_urlsafe(32)

    # Create Stripe customer
    stripe_customer_id = ""
    stripe_subscription_id = ""
    if os.environ.get("STRIPE_SECRET_KEY"):
        stripe_customer_id = billing.create_stripe_customer(tenant_id, body.email, body.name)
        stripe_subscription_id = billing.create_subscription(stripe_customer_id, body.plan)

    with db() as conn:
        # Add stripe columns if they don't exist yet (idempotent migration)
        _ensure_billing_columns(conn)
        conn.execute(
            """
            INSERT INTO tenants (id, api_key, plan, webhook_url, stripe_customer_id, stripe_subscription_id)
            VALUES (?,?,?,?,?,?)
            """,
            (tenant_id, api_key, body.plan, body.webhook_url,
             stripe_customer_id, stripe_subscription_id),
        )

    return {
        "tenant_id": tenant_id,
        "api_key": api_key,
        "plan": body.plan,
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
    }


# ── Stripe webhook ────────────────────────────────────────────────────────────

@router.post("/billing/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    event = billing.parse_webhook(payload, sig)
    if event is None:
        raise HTTPException(status_code=400, detail="invalid signature")

    result = billing.handle_webhook_event(event)

    # Parse customer:XXX:status:YYY style result
    if result.startswith("customer:") and ":status:" in result:
        parts = result.split(":")
        customer_id = parts[1]
        status = parts[3]
        _update_tenant_by_customer(customer_id, status)

    elif "payment_failed" in result:
        customer_id = result.split(":")[1]
        _update_tenant_by_customer(customer_id, "past_due")

    return {"received": True}


def _update_tenant_by_customer(customer_id: str, status: str):
    with db() as conn:
        _ensure_billing_columns(conn)
        conn.execute(
            "UPDATE tenants SET subscription_status=? WHERE stripe_customer_id=?",
            (status, customer_id),
        )


# ── Manual usage sync ─────────────────────────────────────────────────────────

@router.post("/billing/sync")
def billing_sync(x_admin_key: str = Header(...)):
    """Push this month's SMS usage to Stripe for all tenants. Run nightly via cron."""
    if x_admin_key != os.environ.get("ADMIN_KEY", "changeme"):
        raise HTTPException(status_code=401)

    if not os.environ.get("STRIPE_SECRET_KEY"):
        return {"skipped": True, "reason": "STRIPE_SECRET_KEY not set"}

    with db() as conn:
        _ensure_billing_columns(conn)
        tenants = conn.execute(
            "SELECT id, plan, stripe_subscription_id FROM tenants WHERE stripe_subscription_id != ''"
        ).fetchall()

        results = []
        for t in tenants:
            sms_count = conn.execute(
                """
                SELECT COALESCE(SUM(credits),0) FROM usage_ledger
                WHERE tenant_id=? AND direction='outbound'
                  AND strftime('%Y-%m', billed_at) = strftime('%Y-%m', 'now')
                """,
                (t["id"],),
            ).fetchone()[0]

            billing.report_sms_usage(t["stripe_subscription_id"], t["plan"], int(sms_count))
            results.append({"tenant_id": t["id"], "sms_count": int(sms_count)})

    return {"synced": results}


# ── Billing portal URL ────────────────────────────────────────────────────────

@router.get("/v1/billing/portal")
def billing_portal(authorization: str = Header(...)):
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="billing not configured")

    api_key = authorization.removeprefix("Bearer ").strip()
    with db() as conn:
        _ensure_billing_columns(conn)
        row = conn.execute(
            "SELECT stripe_customer_id FROM tenants WHERE api_key=?", (api_key,)
        ).fetchone()

    if not row or not row["stripe_customer_id"]:
        raise HTTPException(status_code=404, detail="no billing customer")

    session = stripe.billing_portal.Session.create(
        customer=row["stripe_customer_id"],
        return_url=os.environ.get("PORTAL_RETURN_URL", "http://localhost:8300"),
    )
    return {"url": session.url}


# ── Schema migration helper ───────────────────────────────────────────────────

def _ensure_billing_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tenants)").fetchall()}
    if "stripe_customer_id" not in cols:
        conn.execute("ALTER TABLE tenants ADD COLUMN stripe_customer_id TEXT DEFAULT ''")
    if "stripe_subscription_id" not in cols:
        conn.execute("ALTER TABLE tenants ADD COLUMN stripe_subscription_id TEXT DEFAULT ''")
    if "subscription_status" not in cols:
        conn.execute("ALTER TABLE tenants ADD COLUMN subscription_status TEXT DEFAULT 'active'")
