"""
Stripe billing integration for SmsSaaS.

Lifecycle:
  1. Admin calls POST /admin/tenants  →  creates Stripe Customer + Subscription
  2. Each SMS send  →  usage_ledger row inserted
  3. Nightly cron (or on /billing/sync)  →  reports usage to Stripe metered price
  4. Stripe invoices monthly, charges card on file
  5. Stripe sends webhook → /billing/webhook → we mark subscription active/cancelled

Products/prices are created once at startup if they don't exist.
"""
import os
import stripe

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

# ---------------------------------------------------------------------------
# Plan → Stripe price IDs  (created once via ensure_stripe_products())
# ---------------------------------------------------------------------------

_PLAN_MONTHLY_PRICE: dict[str, str] = {}   # plan → price_id (recurring)
_SMS_OVERAGE_PRICE:  dict[str, str] = {}   # plan → price_id (metered)

PLANS = {
    "starter":  {"monthly_aud": 2500,  "sms_included": 500,   "overage_cents": 4},
    "pro":      {"monthly_aud": 7900,  "sms_included": 2000,  "overage_cents": 3},
    "business": {"monthly_aud": 19900, "sms_included": 10000, "overage_cents": 2},
}


def ensure_stripe_products():
    """Idempotently create Stripe products + prices for all plans."""
    if not stripe.api_key:
        return  # Stripe not configured — skip silently

    for plan, cfg in PLANS.items():
        # Find or create product
        products = stripe.Product.search(query=f'metadata["smssaas_plan"]:"{plan}"').data
        if products:
            product = products[0]
        else:
            product = stripe.Product.create(
                name=f"SmsSaaS {plan.title()}",
                metadata={"smssaas_plan": plan},
            )

        # Monthly flat fee price
        prices = stripe.Price.search(
            query=f'product:"{product.id}" AND metadata["type"]:"monthly"'
        ).data
        if prices:
            _PLAN_MONTHLY_PRICE[plan] = prices[0].id
        else:
            p = stripe.Price.create(
                product=product.id,
                unit_amount=cfg["monthly_aud"],
                currency="aud",
                recurring={"interval": "month"},
                metadata={"type": "monthly", "smssaas_plan": plan},
            )
            _PLAN_MONTHLY_PRICE[plan] = p.id

        # Metered SMS overage price
        prices = stripe.Price.search(
            query=f'product:"{product.id}" AND metadata["type"]:"sms_overage"'
        ).data
        if prices:
            _SMS_OVERAGE_PRICE[plan] = prices[0].id
        else:
            p = stripe.Price.create(
                product=product.id,
                unit_amount=cfg["overage_cents"],
                currency="aud",
                recurring={"interval": "month", "usage_type": "metered", "aggregate_usage": "sum"},
                metadata={"type": "sms_overage", "smssaas_plan": plan},
            )
            _SMS_OVERAGE_PRICE[plan] = p.id


# ---------------------------------------------------------------------------
# Customer + subscription management
# ---------------------------------------------------------------------------

def create_stripe_customer(tenant_id: str, email: str, name: str) -> str:
    """Create a Stripe customer and return the customer ID."""
    customer = stripe.Customer.create(
        email=email,
        name=name,
        metadata={"smssaas_tenant_id": tenant_id},
    )
    return customer.id


def create_subscription(stripe_customer_id: str, plan: str) -> str:
    """
    Create a Stripe subscription for the given plan.
    Returns the subscription ID.
    Includes both flat monthly price and metered SMS overage.
    """
    if not _PLAN_MONTHLY_PRICE:
        ensure_stripe_products()

    items = [{"price": _PLAN_MONTHLY_PRICE[plan]}]
    if plan in _SMS_OVERAGE_PRICE:
        items.append({"price": _SMS_OVERAGE_PRICE[plan]})

    sub = stripe.Subscription.create(
        customer=stripe_customer_id,
        items=items,
        payment_behavior="default_incomplete",
        expand=["latest_invoice.payment_intent"],
    )
    return sub.id


def cancel_subscription(stripe_subscription_id: str):
    stripe.Subscription.cancel(stripe_subscription_id)


# ---------------------------------------------------------------------------
# Usage reporting
# ---------------------------------------------------------------------------

def report_sms_usage(stripe_subscription_id: str, plan: str, sms_count: int):
    """
    Report metered SMS usage to Stripe for overage billing.
    Only reports the overage above the plan's included quota.
    """
    if not stripe.api_key or sms_count <= 0:
        return
    included = PLANS.get(plan, {}).get("sms_included", 0)
    overage = max(0, sms_count - included)
    if overage == 0:
        return

    # Find the metered subscription item
    sub = stripe.Subscription.retrieve(stripe_subscription_id)
    metered_item_id = None
    for item in sub["items"]["data"]:
        if item["price"]["id"] == _SMS_OVERAGE_PRICE.get(plan):
            metered_item_id = item["id"]
            break

    if not metered_item_id:
        return

    stripe.SubscriptionItem.create_usage_record(
        metered_item_id,
        quantity=overage,
        action="set",  # set absolute count for this billing period
    )


# ---------------------------------------------------------------------------
# Webhook handling
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")


def parse_webhook(payload: bytes, sig_header: str) -> stripe.Event | None:
    try:
        return stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        return None


def handle_webhook_event(event: stripe.Event) -> str:
    """Returns the tenant_id affected, or '' if not relevant."""
    etype = event["type"]
    obj = event["data"]["object"]

    if etype in ("customer.subscription.deleted", "customer.subscription.updated"):
        customer_id = obj.get("customer")
        status = obj.get("status")
        # Caller should update tenants.subscription_status based on this
        return f"customer:{customer_id}:status:{status}"

    if etype == "invoice.payment_failed":
        customer_id = obj.get("customer")
        return f"customer:{customer_id}:payment_failed"

    return ""
