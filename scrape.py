#!/usr/bin/env python3
"""Fetch Gibson Flats units from SightMap and email newly seen listings."""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Any

import requests

SIGHTMAP_ACCOUNT = "40vl66rnvle"
SIGHTMAP_ID = "92878"
SIGHTMAP_URL = (
    f"https://sightmap.com/app/api/v1/{SIGHTMAP_ACCOUNT}/sightmaps/{SIGHTMAP_ID}"
)
LISTINGS_URL = "https://gibsonflats.com/floorplans/"
USER_AGENT = (
    "Mozilla/5.0 (compatible; GibsonFlatsListingWatch/1.0; +https://gibsonflats.com/floorplans/)"
)

ROOT = Path(__file__).resolve().parent
SEEN_PATH = ROOT / "data" / "seen.json"


def fetch_payload() -> dict[str, Any]:
    response = requests.get(
        SIGHTMAP_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def floor_plan_name(plan: dict[str, Any] | None) -> str:
    if not plan:
        return "Unknown"
    raw = plan.get("filter_label") or plan.get("name") or ""
    if isinstance(raw, str) and raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            return str(parsed.get("name") or raw)
        except json.JSONDecodeError:
            return raw
    return str(raw) if raw else "Unknown"


def normalize_units(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") or payload
    plans = {str(plan.get("id")): plan for plan in data.get("floor_plans") or []}
    units: list[dict[str, Any]] = []
    for unit in data.get("units") or []:
        unit_id = str(unit.get("id") or "")
        if not unit_id:
            continue
        plan = plans.get(str(unit.get("floor_plan_id") or ""))
        units.append(
            {
                "id": unit_id,
                "display_unit_number": unit.get("display_unit_number") or f"APT {unit.get('unit_number', unit_id)}",
                "floor_plan": floor_plan_name(plan),
                "beds": (plan or {}).get("bedroom_label") or (plan or {}).get("bedroom_count") or "—",
                "baths": (plan or {}).get("bathroom_label") or (plan or {}).get("bathroom_count") or "—",
                "area": unit.get("display_area") or (f"{unit['area']} sq. ft." if unit.get("area") is not None else "—"),
                "base_rent": unit.get("display_price") or "—",
                "total_monthly": unit.get("total_display_price") or "—",
                "available": unit.get("display_available_on") or unit.get("available_on") or "—",
                "lease_term": unit.get("display_lease_term") or "—",
            }
        )
    units.sort(key=lambda item: (item["display_unit_number"], item["id"]))
    return units


def load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    payload = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    return {str(unit_id) for unit_id in payload.get("unit_ids") or []}


def save_seen(unit_ids: set[str]) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(unit_ids, key=lambda value: (len(value), value))
    SEEN_PATH.write_text(json.dumps({"unit_ids": ordered}, indent=2) + "\n", encoding="utf-8")


def render_html(units: list[dict[str, Any]]) -> str:
    cards = []
    for unit in units:
        cards.append(
            f"""
            <tr>
              <td style="padding:16px 0;border-bottom:1px solid #e6e1d8;">
                <h2 style="margin:0 0 8px;font-size:18px;color:#1f1b16;">
                  {escape(str(unit["display_unit_number"]))}
                  <span style="font-weight:normal;color:#6b6358;"> · {escape(str(unit["floor_plan"]))}</span>
                </h2>
                <p style="margin:0 0 10px;color:#4a453e;font-size:14px;">
                  {escape(str(unit["beds"]))} · {escape(str(unit["baths"]))} · {escape(str(unit["area"]))}
                </p>
                <p style="margin:0;font-size:16px;color:#1f1b16;">
                  <strong>{escape(str(unit["total_monthly"]))}</strong>
                  <span style="color:#6b6358;"> /mo* · {escape(str(unit["base_rent"]))} base rent</span>
                </p>
                <p style="margin:8px 0 0;color:#4a453e;font-size:14px;">
                  {escape(str(unit["available"]))} · {escape(str(unit["lease_term"]))}
                </p>
              </td>
            </tr>
            """
        )
    noun = "listing" if len(units) == 1 else "listings"
    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f4f1ea;font-family:Georgia, 'Times New Roman', serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f1ea;padding:24px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;background:#fffaf3;padding:28px 32px;border-radius:12px;">
          <tr>
            <td>
              <p style="margin:0 0 4px;letter-spacing:0.08em;text-transform:uppercase;font-size:12px;color:#8a8175;">Gibson Flats</p>
              <h1 style="margin:0 0 8px;font-size:24px;color:#1f1b16;">{len(units)} new {noun}</h1>
              <p style="margin:0 0 20px;color:#4a453e;font-size:15px;">
                Newly seen apartments at
                <a href="{LISTINGS_URL}" style="color:#8a5a2b;">{LISTINGS_URL}</a>
              </p>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                {''.join(cards)}
              </table>
              <p style="margin:20px 0 0;font-size:13px;color:#8a8175;">
                Total monthly price includes mandatory monthly fees. Availability and rent change often.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def render_text(units: list[dict[str, Any]]) -> str:
    lines = [f"{len(units)} new Gibson Flats listing(s)", LISTINGS_URL, ""]
    for unit in units:
        lines.extend(
            [
                unit["display_unit_number"],
                f"  Floor plan: {unit['floor_plan']}",
                f"  {unit['beds']} / {unit['baths']} / {unit['area']}",
                f"  {unit['total_monthly']} /mo* ({unit['base_rent']} base rent)",
                f"  {unit['available']} · {unit['lease_term']}",
                "",
            ]
        )
    return "\n".join(lines)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def send_email(units: list[dict[str, Any]]) -> None:
    host = required_env("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = required_env("SMTP_USER")
    password = required_env("SMTP_PASSWORD")
    to_addr = required_env("EMAIL_TO")
    from_addr = os.environ.get("EMAIL_FROM", "").strip() or user

    noun = "listing" if len(units) == 1 else "listings"
    message = EmailMessage()
    message["Subject"] = f"Gibson Flats: {len(units)} new {noun}"
    message["From"] = from_addr
    message["To"] = to_addr
    message.set_content(render_text(units))
    message.add_alternative(render_html(units), subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=60) as smtp:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.login(user, password)
        smtp.send_message(message)


def main() -> int:
    payload = fetch_payload()
    units = normalize_units(payload)
    seen = load_seen()
    new_units = [unit for unit in units if unit["id"] not in seen]

    print(f"Fetched {len(units)} available unit(s); {len(new_units)} new.")
    if not new_units:
        return 0

    send_email(new_units)
    save_seen(seen | {unit["id"] for unit in new_units})
    print(f"Emailed {len(new_units)} listing(s) and updated {SEEN_PATH}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
