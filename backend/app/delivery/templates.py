"""Delivery email bodies (plain text + branded HTML)."""
from __future__ import annotations

from datetime import datetime

from app.config import settings

BRAND = "STREAM CORPORATION"


def render_delivery_email(
    *,
    order,
    item,
    product,
    download_url: str,
    expires_at: datetime,
    has_attachment: bool,
) -> tuple[str, str, str]:
    name = item.product_name if item else "Your product"
    version = (item.product_version if item else None) or (product.version if product else None)
    coins = int(order.coin_total)
    expiry = expires_at.strftime("%d %b %Y %H:%M UTC")
    note = (product.delivery_note if product else None) or ""

    subject = f"{BRAND} — {name} is ready (Order {order.order_code})"

    text = f"""{BRAND}

Your product purchase is confirmed.

Product:
{name}{f" v{version}" if version else ""}

Order ID:
{order.order_code}

Price:
{coins:,} Coins

Your software is ready.

Download:
{download_url}

This secure link is personal to your account and expires on {expiry}.
{"A copy is also attached to this email." if has_attachment else ""}
{note}

Thank you for choosing {BRAND}.
"""

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#05060c;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#e6f1ff">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#05060c;padding:28px 12px">
<tr><td align="center">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:linear-gradient(160deg,#0b1020,#0a0f1c);border:1px solid rgba(0,245,255,.28);border-radius:16px;overflow:hidden">
    <tr><td style="padding:24px 28px;border-bottom:1px solid rgba(0,245,255,.18)">
      <div style="font-size:12px;letter-spacing:.32em;color:#00f5ff;text-transform:uppercase">Stream</div>
      <div style="font-size:24px;font-weight:700;letter-spacing:.08em;color:#fff">CORPORATION</div>
    </td></tr>
    <tr><td style="padding:28px">
      <p style="margin:0 0 18px;font-size:16px;color:#9fb3d9">Your product purchase is confirmed.</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:14px">
        <tr><td style="padding:8px 0;color:#7f8db3">Product</td><td align="right" style="padding:8px 0;color:#fff;font-weight:600">{name}{f" v{version}" if version else ""}</td></tr>
        <tr><td style="padding:8px 0;color:#7f8db3">Order ID</td><td align="right" style="padding:8px 0;color:#00f5ff;font-family:monospace">{order.order_code}</td></tr>
        <tr><td style="padding:8px 0;color:#7f8db3">Price</td><td align="right" style="padding:8px 0;color:#ffd166;font-weight:600">{coins:,} COINS</td></tr>
      </table>
      <div style="margin:26px 0;text-align:center">
        <a href="{download_url}" style="display:inline-block;padding:14px 34px;border-radius:10px;background:linear-gradient(90deg,#00f5ff,#7b5cff);color:#04060f;font-weight:700;text-decoration:none;letter-spacing:.06em">SECURE DOWNLOAD</a>
      </div>
      <p style="margin:0 0 8px;font-size:12px;color:#7f8db3">This link is bound to your account and expires on {expiry}.</p>
      {'<p style="margin:0 0 8px;font-size:12px;color:#7f8db3">A copy of the file is attached to this email.</p>' if has_attachment else ''}
      {f'<p style="margin:14px 0 0;font-size:13px;color:#9fb3d9">{note}</p>' if note else ''}
    </td></tr>
    <tr><td style="padding:18px 28px;border-top:1px solid rgba(0,245,255,.18);font-size:11px;color:#5c6a8a">
      Thank you for choosing {BRAND}. · <a href="{settings.base_url}" style="color:#00f5ff;text-decoration:none">{settings.base_url}</a>
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""

    return subject, text, html


def render_receipt_email(*, user_label: str, coins: int, balance: int, reference: str) -> tuple[str, str, str]:
    subject = f"{BRAND} — {coins:,} coins added to your wallet"
    text = (
        f"{BRAND}\n\nHello {user_label},\n\nPayment confirmed successfully.\n\n"
        f"{coins:,} Coins have been added to your wallet.\n"
        f"Your updated balance is: {balance:,} Coins\n\nReference: {reference}\n"
    )
    html = f"""<div style="font-family:Segoe UI,Arial,sans-serif;background:#05060c;color:#e6f1ff;padding:24px">
<h2 style="color:#00f5ff;letter-spacing:.1em;margin:0 0 12px">{BRAND}</h2>
<p>Payment confirmed successfully.</p>
<p style="font-size:22px;color:#ffd166;font-weight:700">+{coins:,} COINS</p>
<p>Updated balance: <strong>{balance:,} Coins</strong></p>
<p style="font-size:12px;color:#7f8db3">Reference {reference}</p></div>"""
    return subject, text, html
