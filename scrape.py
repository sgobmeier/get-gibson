#!/usr/bin/env python3
"""Fetch Gibson Flats units from SightMap and email newly seen listings."""

from __future__ import annotations

import json
import os
import re
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
SITE_ORIGIN = "https://gibsonflats.com"
USER_AGENT = (
    "Mozilla/5.0 (compatible; GibsonFlatsListingWatch/1.0; +https://gibsonflats.com/floorplans/)"
)

ROOT = Path(__file__).resolve().parent
SEEN_PATH = ROOT / "data" / "seen.json"
EMAIL_TEMPLATE_PATH = ROOT / "templates" / "email.html"
CARD_TEMPLATE_PATH = ROOT / "templates" / "email_card.html"
PREVIEW_PATH = ROOT / "email_preview.html"
FOOTER_IMAGE_PATH = ROOT / "public" / "4hufP.png"
FOOTER_IMAGE_CID = "footer-image"


def fetch_payload() -> dict[str, Any]:
    response = requests.get(
        SIGHTMAP_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def fetch_site_unit_extras() -> dict[str, dict[str, str]]:
    """Map apartment numbers to gibsonflats.com permalinks and thumbnails."""
    response = requests.get(
        LISTINGS_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        timeout=60,
    )
    response.raise_for_status()
    match = re.search(
        r'<script type="application/json" id="jd-fp-data-script-app">(.*?)</script>',
        response.text,
        re.S,
    )
    if not match:
        return {}

    data = json.loads(match.group(1))
    extras: dict[str, dict[str, str]] = {}
    for unit in data.get("units") or []:
        number = str(unit.get("apartment_number") or "").strip()
        if not number:
            continue
        permalink = unit.get("permalink") or ""
        thumbnail = (unit.get("thumbnail") or {}).get("src") or ""
        extras[number] = {
            "listing_url": f"{SITE_ORIGIN}{permalink}" if permalink.startswith("/") else permalink,
            "image_url": thumbnail,
        }
    return extras


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


def normalize_units(
    payload: dict[str, Any],
    site_extras: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    data = payload.get("data") or payload
    plans = {str(plan.get("id")): plan for plan in data.get("floor_plans") or []}
    site_extras = site_extras or {}
    units: list[dict[str, Any]] = []
    for unit in data.get("units") or []:
        unit_id = str(unit.get("id") or "")
        if not unit_id:
            continue
        plan = plans.get(str(unit.get("floor_plan_id") or ""))
        number = str(unit.get("unit_number") or "").strip()
        extras = site_extras.get(number, {})
        image_url = (
            extras.get("image_url")
            or unit.get("view_image_url")
            or (plan or {}).get("image_url")
            or ""
        )
        listing_url = extras.get("listing_url") or LISTINGS_URL
        units.append(
            {
                "id": unit_id,
                "display_unit_number": unit.get("display_unit_number")
                or f"APT {unit.get('unit_number', unit_id)}",
                "floor_plan": floor_plan_name(plan),
                "beds": (plan or {}).get("bedroom_label") or (plan or {}).get("bedroom_count") or "—",
                "baths": (plan or {}).get("bathroom_label") or (plan or {}).get("bathroom_count") or "—",
                "area": unit.get("display_area")
                or (f"{unit['area']} sq. ft." if unit.get("area") is not None else "—"),
                "base_rent": unit.get("display_price") or "—",
                "total_monthly": unit.get("total_display_price") or "—",
                "available": unit.get("display_available_on") or unit.get("available_on") or "—",
                "lease_term": unit.get("display_lease_term") or "—",
                "image_url": image_url,
                "listing_url": listing_url,
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


def fill_template(template: str, values: dict[str, Any]) -> str:
    result = template
    for key, value in values.items():
        result = result.replace(f"{{{{{key}}}}}", str(value))
    return result


def render_card(unit: dict[str, Any], card_template: str) -> str:
    return fill_template(
        card_template,
        {
            "display_unit_number": escape(str(unit["display_unit_number"])),
            "floor_plan": escape(str(unit["floor_plan"])),
            "beds": escape(str(unit["beds"])),
            "baths": escape(str(unit["baths"])),
            "area": escape(str(unit["area"])),
            "total_monthly": escape(str(unit["total_monthly"])),
            "base_rent": escape(str(unit["base_rent"])),
            "available": escape(str(unit["available"])),
            "lease_term": escape(str(unit["lease_term"])),
            "image_url": escape(str(unit["image_url"]), quote=True),
            "listing_url": escape(str(unit["listing_url"]), quote=True),
        },
    )


def render_html(units: list[dict[str, Any]], *, footer_image_src: str | None = None) -> str:
    email_template = EMAIL_TEMPLATE_PATH.read_text(encoding="utf-8")
    card_template = CARD_TEMPLATE_PATH.read_text(encoding="utf-8")
    cards = "".join(render_card(unit, card_template) for unit in units)
    noun = "listing" if len(units) == 1 else "listings"
    if footer_image_src is None:
        footer_image_src = FOOTER_IMAGE_PATH.relative_to(ROOT).as_posix()
    return fill_template(
        email_template,
        {
            "count": len(units),
            "noun": noun,
            "listings_url": LISTINGS_URL,
            "cards": cards,
            "footer_image_src": escape(footer_image_src, quote=True),
        },
    )


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
                f"  {unit['listing_url']}",
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

    html = render_html(units, footer_image_src=f"cid:{FOOTER_IMAGE_CID}")
    message.add_alternative(html, subtype="html")
    if FOOTER_IMAGE_PATH.exists():
        message.get_payload()[-1].add_related(
            FOOTER_IMAGE_PATH.read_bytes(),
            maintype="image",
            subtype="png",
            cid=FOOTER_IMAGE_CID,
            filename=FOOTER_IMAGE_PATH.name,
        )

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=60) as smtp:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.login(user, password)
        smtp.send_message(message)


def write_preview(units: list[dict[str, Any]]) -> Path:
    PREVIEW_PATH.write_text(render_html(units), encoding="utf-8")
    return PREVIEW_PATH


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    preview_only = "--preview" in args

    payload = fetch_payload()
    site_extras = fetch_site_unit_extras()
    units = normalize_units(payload, site_extras)

    if preview_only:
        path = write_preview(units)
        print(f"Wrote preview with {len(units)} unit(s) to {path}")
        return 0

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
