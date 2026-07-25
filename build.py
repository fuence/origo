#!/usr/bin/env python3
"""build.py — generates products.json, feed.xml, dispatch/paste-packs/*.txt,
and the 4 legal pages (terms/returns/privacy/company .html) from
products.yaml + config/legal-texts/*.md.

products.yaml is the single source of truth. Never hand-edit the generated
outputs below — edit products.yaml and re-run this script (`make build` or
`python3 build.py`).

products.yaml deliberately does NOT live in this repo (origo-site gets
pushed to public GitHub Pages; products.yaml has real supplier/cost/margin
data). It lives in the private origo-ops repo (Gitea) instead — this
script reads it from there via ORIGO_PRODUCTS_YAML (default:
/mnt/nas-work/origo/products.yaml). Only the generated, already
public-safe outputs below are written into this repo.

Legal pages (2026-07-22, legal completeness pass): terms.html/
returns.html/privacy.html/company.html are generated from
config/legal-texts/{terms,returns,privacy,company}.md (origo-ops) —
never hand-edit the .html output, edit the .md source and rebuild. A
missing .md source is a HARD build failure (see main()) — unlike the
food_info gate below, which stays a per-product exclusion, not a build
failure (Elijo's call, 2026-07-22: hard-failing on food_info would trip
the auto-rebuild cron on nearly every catalog edit until all 6 known-
incomplete SKUs are fixed; a missing legal-text file is a one-time,
controllable condition with no such recurring-noise risk).
"""
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
PRODUCTS_YAML_PATH = Path(os.environ.get("ORIGO_PRODUCTS_YAML", "/mnt/nas-work/origo/products.yaml"))
CONFIG_YAML_PATH = Path(os.environ.get("ORIGO_CONFIG_YAML", "/mnt/nas-work/origo/config/config.yaml"))
SHIPPING_YAML_PATH = Path(os.environ.get("ORIGO_SHIPPING_YAML", "/mnt/nas-work/origo/config/shipping.yaml"))
LEGAL_TEXTS_DIR = Path(os.environ.get("ORIGO_LEGAL_TEXTS_DIR", "/mnt/nas-work/origo/config/legal-texts"))
ACCOUNTS_REGISTRY_PATH = Path(os.environ.get("ORIGO_ACCOUNTS_REGISTRY", "/mnt/nas-work/origo/config/accounts-registry.yaml"))
FOOD_INFO_GAPS_PATH = Path(os.environ.get("ORIGO_FOOD_INFO_GAPS", "/mnt/nas-work/origo/state/food-info-gaps.json"))
SUPPLIERS_DIR_PATH = Path(os.environ.get("ORIGO_SUPPLIERS_DIR", "/mnt/nas-work/origo/suppliers"))
PROCUREMENT_BLOCKERS_PATH = Path(os.environ.get("ORIGO_PROCUREMENT_BLOCKERS", "/mnt/nas-work/origo/state/procurement-blockers.json"))
SITE_URL = "https://iieik.eu"

# EU Reg 1169/2011 (Food Information to Consumers) applies to these
# products.yaml `category` values — food supplements (health) are "food"
# for FIC labelling purposes even though a supplement's nutrition
# declaration itself can be exempt (Annex V point 17 — see
# food_info.nutrition_declaration_exempt).
FOOD_INFO_CATEGORIES = {"food", "health"}

# Maps products.yaml's `category` field to a skelbimai.lt category
# suggestion. Not authoritative — Elijo should adjust per listing if
# skelbimai's own category tree suggests something more specific.
SKELBIMAI_CATEGORY = {
    "food": "Maistas ir gėrimai",
    "cookware": "Namai ir sodas / Virtuvės reikmenys",
    "health": "Sveikata ir grožis",
    "tech": "Kompiuterinė technika",
}


def skelbimai_category(product: dict) -> str:
    return SKELBIMAI_CATEGORY.get(product.get("category"), "Kita")


def load_products() -> list[dict]:
    if not PRODUCTS_YAML_PATH.is_file():
        sys.exit(
            f"ERROR: products.yaml not found at {PRODUCTS_YAML_PATH}\n"
            f"(set ORIGO_PRODUCTS_YAML if it lives somewhere else — see build.py's docstring)"
        )
    data = yaml.safe_load(PRODUCTS_YAML_PATH.read_text(encoding="utf-8"))
    products = data.get("products", [])
    if not products:
        print("WARNING: products.yaml has zero products", file=sys.stderr)
    return products


def in_stock(product: dict) -> bool:
    stock = product.get("stock")
    return bool(stock and stock > 0)


def live_products(products: list[dict]) -> list[dict]:
    return [p for p in products if p.get("status") == "live"]


def load_config() -> dict:
    if not CONFIG_YAML_PATH.is_file():
        return {}
    return yaml.safe_load(CONFIG_YAML_PATH.read_text(encoding="utf-8")) or {}


def load_shipping_config() -> dict:
    if not SHIPPING_YAML_PATH.is_file():
        return {"configured": False, "free_shipping_threshold_eur": None, "couriers": []}
    return yaml.safe_load(SHIPPING_YAML_PATH.read_text(encoding="utf-8")) or {}


_return_terms_cache: dict = {}


def load_return_terms_text(key: str) -> dict:
    """Resolves a products.yaml return_terms/withdrawal_terms enum value to
    its trilingual text via config/legal-texts/return-terms/{key}.yaml —
    both fields share the same enum/files (see products.yaml's header
    comment). Cached per build run — the same handful of keys get looked
    up once per product."""
    if key in _return_terms_cache:
        return _return_terms_cache[key]
    path = LEGAL_TEXTS_DIR / "return-terms" / f"{key}.yaml"
    if not path.is_file():
        text = {"lt": None, "en": None, "pl": None}
    else:
        text = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    _return_terms_cache[key] = text
    return text


_guarantee_cache: dict = {}


def load_guarantee_text(key: str) -> dict:
    """Resolves a products.yaml guarantee enum value (Track 2 / track:
    exclusive products only) to its trilingual text via
    config/legal-texts/guarantee/{key}.yaml."""
    if key in _guarantee_cache:
        return _guarantee_cache[key]
    path = LEGAL_TEXTS_DIR / "guarantee" / f"{key}.yaml"
    if not path.is_file():
        text = {"lt": None, "en": None, "pl": None}
    else:
        text = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    _guarantee_cache[key] = text
    return text


_secondhand_disclosure_cache: dict = None


def load_secondhand_disclosure_text() -> dict:
    """config/legal-texts/secondhand-disclosure.yaml — auto-injected by
    build.py into every generated listing format for any product with
    condition: opened-tested (see products.yaml's header comment).
    Cached module-wide since it's the same file for every product."""
    global _secondhand_disclosure_cache
    if _secondhand_disclosure_cache is None:
        path = LEGAL_TEXTS_DIR / "secondhand-disclosure.yaml"
        if not path.is_file():
            _secondhand_disclosure_cache = {"lt": None, "en": None, "pl": None}
        else:
            _secondhand_disclosure_cache = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _secondhand_disclosure_cache


_seal_notice_cache: dict = None


def load_seal_notice_text() -> dict:
    """config/legal-texts/seal-notice.yaml — PATCH v3 (2026-07-22),
    auto-injected by build.py into every generated listing format for any
    product with sealed: true (see products.yaml's header comment).
    Cached module-wide since it's the same file for every product."""
    global _seal_notice_cache
    if _seal_notice_cache is None:
        path = LEGAL_TEXTS_DIR / "seal-notice.yaml"
        if not path.is_file():
            _seal_notice_cache = {"lt": None, "en": None, "pl": None}
        else:
            _seal_notice_cache = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _seal_notice_cache


# ── Track 2 (track: exclusive) withdrawal/guarantee/condition validation ───
# Hard build failure (matching build_legal_pages()'s pattern) — this is a
# real compliance guard Elijo asked for explicitly (2026-07-22), not
# advisory: a Track 2 product's guarantee claim must actually match its
# real condition, with no silent fallback.
TRACK2_WITHDRAWAL_TERMS_VALUES = {"standard-14d", "food-sealed", "perishable-no-withdrawal"}
TRACK2_GUARANTEE_VALUES = {"new-2y", "secondhand-1y-agreed", "b2b-only"}
TRACK2_CONDITION_VALUES = {"new", "opened-tested"}


def validate_track2_terms(products: list[dict]) -> None:
    errors = []
    for p in products:
        if p.get("track") != "exclusive":
            continue
        sku = p.get("sku", "?")
        withdrawal_terms = p.get("withdrawal_terms")
        guarantee = p.get("guarantee")
        condition = p.get("condition")
        if withdrawal_terms not in TRACK2_WITHDRAWAL_TERMS_VALUES:
            errors.append(f"{sku}: withdrawal_terms must be one of {sorted(TRACK2_WITHDRAWAL_TERMS_VALUES)}, got {withdrawal_terms!r}")
        if guarantee not in TRACK2_GUARANTEE_VALUES:
            errors.append(f"{sku}: guarantee must be one of {sorted(TRACK2_GUARANTEE_VALUES)}, got {guarantee!r}")
        if condition not in TRACK2_CONDITION_VALUES:
            errors.append(f"{sku}: condition must be one of {sorted(TRACK2_CONDITION_VALUES)}, got {condition!r}")
            continue
        if condition != "opened-tested" and guarantee != "new-2y":
            errors.append(
                f"{sku}: Track 2 NEW goods (condition != opened-tested) must have guarantee: new-2y always, "
                f"no exceptions — got guarantee: {guarantee!r} with condition: {condition!r}"
            )
        if guarantee == "secondhand-1y-agreed" and condition != "opened-tested":
            errors.append(
                f"{sku}: guarantee: secondhand-1y-agreed is only allowed when condition: opened-tested is set "
                f"— got condition: {condition!r}"
            )
    if errors:
        sys.exit(
            "ERROR: Track 2 (track: exclusive) withdrawal_terms/guarantee/condition validation failed:\n"
            + "\n".join(f"  - {e}" for e in errors)
            + "\nFix products.yaml (origo-ops repo) — see its header comment for the field reference."
        )


# ── single-source policy (Segment P1, 2026-07-23) ──────────────────────────
# docs/ORIGO-procurement-system-FINAL.md sec.1: "No SKU goes status: live
# with fewer than two suppliers at stage >= quoted." A supplier "covers" a
# SKU if either its supplier_ref matches the product's own supplier_ref
# (the primary/established link) or the SKU appears in the supplier's
# free-form products[] list (a candidate/alternate-source mention).
SINGLE_SOURCE_GATE_STAGES = {"quoted", "sampling", "approved", "active"}


def load_suppliers() -> list[dict]:
    if not SUPPLIERS_DIR_PATH.is_dir():
        return []
    out = []
    for d in sorted(SUPPLIERS_DIR_PATH.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        p = d / "supplier.yaml"
        if p.is_file():
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if data:
                out.append(data)
    return out


def check_single_source_policy(products: list[dict], suppliers: list[dict], config: dict) -> list[str]:
    """Returns a list of human-readable warnings (or raises via sys.exit if
    config.yaml -> sourcing.single_source_policy is "block" instead of the
    default "warn"). Deliberately non-fatal by default -- as of 2026-07-23
    almost no SKU has 2 real quoted-or-further suppliers yet, so "block"
    would halt nearly every build."""
    policy = ((config.get("sourcing") or {}).get("single_source_policy") or "warn").strip().lower()
    warnings = []
    for p in live_products(products):
        sku = p.get("sku")
        primary_ref = p.get("supplier_ref")
        covering = [
            s for s in suppliers
            if s.get("status") in SINGLE_SOURCE_GATE_STAGES
            and (s.get("supplier_ref") == primary_ref or sku in (s.get("products") or []))
        ]
        if len(covering) < 2:
            names = ", ".join(s.get("name", s.get("slug", "?")) for s in covering) or "none"
            warnings.append(f"{sku}: only {len(covering)} supplier(s) at stage>=quoted ({names}) — single-source risk")
    if warnings and policy == "block":
        sys.exit(
            "ERROR: single-source policy is 'block' and these live SKUs have fewer than 2 quoted-or-further suppliers:\n"
            + "\n".join(f"  - {w}" for w in warnings)
        )
    return warnings


def build_procurement_blockers(gaps_report: dict, suppliers: list[dict]) -> dict:
    """state/procurement-blockers.json (Segment P1) -- the bridge between
    the food_info gate above and the sourcing/RFQ funnel: for each
    food_info-blocked SKU, which suppliers were asked (via
    inquiries-log.csv rows whose product field mentions the SKU) and when,
    so it's visible from one file whether a blocker is "nobody's asked
    yet" vs. "asked, still waiting"."""
    inquiries_path = SUPPLIERS_DIR_PATH / "inquiries-log.csv"
    inquiry_rows = []
    if inquiries_path.is_file():
        import csv as _csv
        with open(inquiries_path, newline="", encoding="utf-8") as f:
            inquiry_rows = list(_csv.DictReader(f))

    blockers = {}
    for sku, gaps in gaps_report.items():
        asked = [
            {"date": row.get("date", ""), "supplier_name": row.get("supplier_name", ""),
             "alias": row.get("alias", ""), "src_token": row.get("src_token", ""),
             "response_received": row.get("response_received", "")}
            for row in inquiry_rows
            if sku in (row.get("product") or "")
        ]
        # NOTE: "reply_received" here means the general inquiry thread got
        # a reply (inquiries-log.csv's response_received column) -- it does
        # NOT mean the SPECIFIC missing fields listed in outstanding_fields
        # were actually provided in that reply. This is a crude SKU-in-
        # product-text cross-reference, not mailbox content parsing (that
        # would need Segment M2's inbound automation, which is parked).
        # "reply_received_verify_data" is a prompt to check by hand, not a
        # claim the gap is already closed.
        if not asked:
            status = "not_yet_asked"
        elif all(a["response_received"].lower() not in ("yes", "y", "true") for a in asked):
            status = "awaiting_reply"
        else:
            status = "reply_received_verify_data"
        blockers[sku] = {
            "outstanding_fields": gaps,
            "suppliers_asked": asked,
            "status": status,
        }
    return blockers


# ── food_info completeness gate (EU Reg 1169/2011) ─────────────────────────
# A live food/health product missing ANY required field below (or whose
# importer_or_seller_details_ref can't be resolved to a filled-in business
# address) is excluded from products.json entirely — never published
# half-compliant. This is a real, expected outcome right now: no product
# in this catalog has real supplier ingredient/nutrition/storage data on
# file, so every food/health product will fail this gate until that data
# is chased down. See products.yaml's header comment + docs/EDITING.md.
def _trilingual_complete(d) -> bool:
    if not isinstance(d, dict):
        return False
    return all(isinstance(d.get(lang), str) and d.get(lang).strip() for lang in ("lt", "en", "pl"))


def food_info_gaps(product: dict, config: dict) -> list[str]:
    """Returns a list of human-readable gap descriptions; empty list = complete."""
    gaps = []
    fi = product.get("food_info")
    if not isinstance(fi, dict):
        return ["food_info block missing entirely"]

    if not _trilingual_complete(fi.get("ingredients")):
        gaps.append("ingredients (lt/en/pl) missing")
    if not _trilingual_complete(fi.get("allergens")):
        gaps.append("allergens (lt/en/pl) missing")
    if not _trilingual_complete(fi.get("storage_conditions")):
        gaps.append("storage_conditions (lt/en/pl) missing")
    if not _trilingual_complete(fi.get("origin_country")):
        gaps.append("origin_country (lt/en/pl) missing")
    net_quantity = fi.get("net_quantity")
    if not (isinstance(net_quantity, str) and net_quantity.strip()):
        gaps.append("net_quantity missing")

    if not fi.get("nutrition_declaration_exempt"):
        nutrition = fi.get("nutrition_per_100g") or {}
        required_keys = ("energy_kj", "energy_kcal", "fat", "saturates", "carbs", "sugars", "protein", "salt")
        missing_keys = [k for k in required_keys if nutrition.get(k) is None]
        if missing_keys:
            gaps.append(f"nutrition_per_100g missing: {', '.join(missing_keys)}")

    ref = fi.get("importer_or_seller_details_ref")
    if not ref:
        gaps.append("importer_or_seller_details_ref missing")
    else:
        address = (config.get("business", {}) or {}).get("registered_address", "")
        if not (address or "").strip():
            gaps.append("config.yaml -> business.registered_address is blank (blocks EVERY food/health product)")

    return gaps


# ── products.json — public-safe subset for the site's product cards ────────
# Deliberately excludes: cost, supplier_ref, reorder_point, batch_lot,
# import_vat_paid, track, platforms, internal_notes, weight_grams — all
# internal/operational, never published. `stock` (a real count) becomes
# `in_stock` (a boolean) so the actual on-hand quantity is never exposed
# either. `icon`/`origin`/`category`/`delivery_estimate` ARE included —
# they're existing public-facing design elements (card background,
# 📍 origin line, 🚚 delivery estimate), not internal data.
#
# food_info gate (2026-07-22): a live food/health product with an
# incomplete food_info block is skipped entirely — see food_info_gaps().
# Returns (data, gaps_report) where gaps_report is {sku: [gap, ...]} for
# every food/health product that got excluded, so the caller can write it
# out prominently rather than the omission going unnoticed.
def build_products_json(products: list[dict], config: dict) -> tuple[dict, dict]:
    items = []
    gaps_report = {}
    for p in live_products(products):
        if not p.get("platforms", {}).get("website", True):
            continue
        if p.get("category") in FOOD_INFO_CATEGORIES:
            gaps = food_info_gaps(p, config)
            if gaps:
                gaps_report[p["sku"]] = gaps
                continue

        entry = {
            "sku": p["sku"],
            "name": p["name"],
            "desc": p["desc"],
            "origin": p.get("origin"),
            "category": p.get("category"),
            "icon": p.get("icon"),
            "delivery_estimate": p.get("delivery_estimate"),
            "track": p.get("track"),
            "price_eur": p["price_eur"],
            "in_stock": in_stock(p),
            "photos": p.get("photos", []),
            "best_before": p.get("best_before"),
            "weight_grams": p.get("weight_grams"),
        }
        # Tamper-seal notice (PATCH v3, 2026-07-22) -- independent of the
        # Track 1/Track 2 branch below, since sealed is decoupled from
        # return_terms/withdrawal_terms (see products.yaml's header comment).
        if p.get("sealed"):
            entry["sealed"] = True
            entry["seal_notice_text"] = load_seal_notice_text()
        # Track 2 (track: exclusive) products carry withdrawal_terms/
        # guarantee/condition INSTEAD OF return_terms -- see products.yaml's
        # header comment. validate_track2_terms() (called in main(), before
        # this function runs) already guarantees these three fields are
        # present and consistent for every track: exclusive product here.
        if p.get("track") == "exclusive":
            condition = p.get("condition", "new")
            entry["withdrawal_terms"] = p.get("withdrawal_terms", "")
            entry["withdrawal_terms_text"] = load_return_terms_text(p.get("withdrawal_terms", ""))
            entry["guarantee"] = p.get("guarantee", "")
            entry["guarantee_text"] = load_guarantee_text(p.get("guarantee", ""))
            entry["condition"] = condition
            if condition == "opened-tested":
                entry["secondhand_disclosure_text"] = load_secondhand_disclosure_text()
        else:
            return_terms_key = p.get("return_terms", "")
            entry["return_terms"] = return_terms_key
            entry["return_terms_text"] = load_return_terms_text(return_terms_key)
        if p.get("category") in FOOD_INFO_CATEGORIES:
            food_info_public = dict(p["food_info"])
            business = config.get("business", {}) or {}
            # Resolves importer_or_seller_details_ref -- only one ref value
            # ("default") exists today, so this is a flat lookup rather than
            # a real dict-of-refs; see food_info_gaps() for the completeness
            # check that guarantees registered_address is non-blank by the
            # time a food product actually reaches this line.
            food_info_public["seller_details"] = {
                "name": business.get("name", "ORIGO"),
                "address": business.get("registered_address", ""),
            }
            entry["food_info"] = food_info_public
        items.append(entry)
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "products": items,
        "shipping": load_shipping_config(),
        # Checkout payment-method gating (2026-07-25) -- see config.yaml's
        # own payment_methods comment for what enabling gateway actually
        # requires. Defaults match index.html's own fallback if this key
        # is ever missing (e.g. an older config.yaml on a stale clone).
        "payment_methods": config.get("payment_methods", {
            "bank_transfer": {"enabled": True},
            "gateway": {"enabled": False, "provider": None},
        }),
    }, gaps_report


# ── feed.xml — price-comparison feed for kainos.lt / kaina24.lt ────────────
# SCHEMA: Google Merchant / RSS 2.0 product feed (the "g:" namespace,
# http://base.google.com/ns/1.0). This is the common-denominator format
# publicly documented as accepted by Lithuanian price-comparison engines
# (kainos.lt, kaina24.lt) for merchant feed import, alongside their own
# proprietary CSV/XML variants.
#
# NOT independently re-verified against each site's current merchant-portal
# docs for this build — Claude Code is network-blocked from fetching
# external sites (CLAUDE.md Phase 1 "cloud/outbound HTTPS blocked" rule).
# Elijo has real merchant-account access to kainos.lt/kaina24.lt; before
# relying on this feed for a real submission, check their current portal
# docs for: exact required/optional g: fields, the URL they expect to poll
# this feed from, and how often they expect it to refresh.
#
# NOTE (Marketing Studio, 2026-07-21): inclusion is gated ONLY on
# kainos/kaina24 — the separate kaina/pigu per-product flags are NOT
# independently honored (no per-platform feed exists, just this one shared
# file). This is a real, pre-existing gap: a product flagged kaina:true or
# pigu:true but kainos:false/kaina24:false won't actually appear here
# despite the flag saying "listed" — left as-is (not fixed) since fixing it
# is a separate decision (per-platform feeds vs. relaxing this gate), the
# ORIGO app's MARKETING channel board surfaces the mismatch honestly rather
# than hiding it (see origo-app-state.py's build_marketing()).
def build_feed_xml(products: list[dict], excluded_skus: set[str]) -> tuple[str, set[str]]:
    def esc(s):
        return html.escape(str(s), quote=True)

    items_xml = []
    included_skus = set()
    for p in live_products(products):
        if p["sku"] in excluded_skus:
            continue
        plats = p.get("platforms", {})
        if not (plats.get("kainos") or plats.get("kaina24")):
            continue
        included_skus.add(p["sku"])
        photos = p.get("photos") or []
        image_tag = f"<g:image_link>{esc(SITE_URL + '/photos/' + p['sku'] + '/' + photos[0])}</g:image_link>" if photos else ""
        availability = "in stock" if in_stock(p) else "out of stock"
        # g:condition (Google Merchant schema: new | refurbished | used) --
        # was hardcoded "new" for every product, which was WRONG for any
        # Track 2 secondhand unit (see products.yaml's condition field).
        # Secondhand disclosure (2026-07-22) is auto-appended to the
        # description too, same as every other generated listing format --
        # never relies on the product's own desc wording alone.
        secondhand = p.get("track") == "exclusive" and p.get("condition") == "opened-tested"
        g_condition = "used" if secondhand else "new"
        description_lt = p["desc"]["lt"]
        if secondhand:
            disclosure_lt = load_secondhand_disclosure_text().get("lt")
            if disclosure_lt:
                description_lt = f"{description_lt} {disclosure_lt}"
        if p.get("sealed"):
            seal_lt = load_seal_notice_text().get("lt")
            if seal_lt:
                description_lt = f"{description_lt} {seal_lt}"
        items_xml.append(f"""    <item>
      <g:id>{esc(p['sku'])}</g:id>
      <title>{esc(p['name']['lt'])}</title>
      <description>{esc(description_lt)}</description>
      <link>{esc(SITE_URL + '/#' + p['sku'])}</link>
      <g:price>{p['price_eur']:.2f} EUR</g:price>
      <g:availability>{availability}</g:availability>
      <g:condition>{g_condition}</g:condition>
      <g:brand>ORIGO</g:brand>
      {image_tag}
    </item>""")

    body = "\n".join(items_xml)
    xml_text = f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated by build.py from products.yaml — do not hand-edit.
     Schema: Google Merchant / RSS 2.0 product feed (g: namespace),
     the common denominator format for LT price-comparison engines
     (kainos.lt, kaina24.lt). See build.py's build_feed_xml() docstring
     for the verification caveat before relying on this for real submission. -->
<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">
  <channel>
    <title>ORIGO product feed</title>
    <link>{esc(SITE_URL)}</link>
    <description>ORIGO ({esc(SITE_URL)}) price-comparison product feed</description>
{body}
  </channel>
</rss>
"""
    return xml_text, included_skus


# ── dispatch/paste-packs/{sku}-skelbimai.txt — one manual-paste text per live product ──
def build_paste_pack(p: dict) -> str:
    photos = p.get("photos") or []
    photo_line = ", ".join(photos) if photos else "(nėra nuotraukų — pridėti prieš skelbimą)"
    price_str = f"{p['price_eur']:.2f}".replace(".", ",") + " €"
    origin = p.get("origin", {}).get("lt")
    origin_line = f"\nKilmė: {origin}" if origin else ""
    # Secondhand disclosure (2026-07-22) -- auto-injected prominently,
    # same as feed.xml/products.json, for any Track 2 (track: exclusive)
    # product with condition: opened-tested. Never relies on the
    # product's own desc wording alone -- see products.yaml's header
    # comment.
    disclosure_line = ""
    if p.get("track") == "exclusive" and p.get("condition") == "opened-tested":
        disclosure_lt = load_secondhand_disclosure_text().get("lt")
        if disclosure_lt:
            disclosure_line = f"\n\n{disclosure_lt}"
    # Tamper-seal notice (PATCH v3, 2026-07-22) -- same auto-inject
    # convention as the secondhand disclosure above, independent of it.
    seal_line = ""
    if p.get("sealed"):
        seal_lt = load_seal_notice_text().get("lt")
        if seal_lt:
            seal_line = f"\n\n{seal_lt}"
    return f"""ANTRAŠTĖ:
{p['name']['lt']}

APRAŠYMAS:
{p['desc']['lt']}{origin_line}{disclosure_line}{seal_line}

KAINA:
{price_str}

NUOTRAUKOS:
{photo_line}

SIŪLOMA KATEGORIJA:
{skelbimai_category(p)}

SKU (vidinis, nerodyti skelbime): {p['sku']}
"""



# ── site-outputs-mirror.json — lets the ORIGO app's MARKETING channel board
# see what this repo ACTUALLY generated, without the origo-site repo being
# reachable from wherever it reads state from. ────────────────────────────
# origo-app-state.py runs inside the NAS's nas-launcher container, which
# mounts /Volume2/Work at /mnt/nas-work — the same volume as origo-ops
# (this script already reads products.yaml from there via
# ORIGO_PRODUCTS_YAML) — but origo-site itself only ever lives on
# Thinktank (docs/RUNBOOK.md -> Catalog), so that container can't read
# feed.xml/products.json/paste-packs directly. Writing a small derived-
# metadata mirror into origo-ops's own state/ dir (this script already
# crosses that same NFS boundary to READ products.yaml, so writing a
# summary back is symmetric, not a new dependency) solves it the same way
# origo-catalog-edit.py keeps its own catalog-full.json mirror rather than
# importing reconcile.py across repos.
SITE_MIRROR_PATH = Path(os.environ.get("ORIGO_SITE_MIRROR", "/mnt/nas-work/origo/state/site-outputs-mirror.json"))


def write_site_outputs_mirror(products_json_data, feed_skus, paste_pack_skus):
    SITE_MIRROR_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = SITE_MIRROR_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps({
        "generated_at": products_json_data["generated_at"],
        "products_json_skus": sorted(p["sku"] for p in products_json_data["products"]),
        "feed_xml_skus": sorted(feed_skus),
        "paste_pack_skus": sorted(paste_pack_skus),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(SITE_MIRROR_PATH)


# ── Legal pages (2026-07-22) ────────────────────────────────────────────────
# terms.html / returns.html / privacy.html / company.html, generated from
# config/legal-texts/{terms,returns,privacy,company}.md. Each .md source
# holds all 3 languages in one file, delimited by "::: lt" / "::: en" /
# "::: pl" ... ":::" blocks (see terms.md's own header comment for the
# full authoring convention). A small purpose-built markdown-lite ->
# HTML converter is used instead of a new pip dependency, matching this
# project's existing no-framework/minimal-deps ethos (index.html has zero
# JS dependencies, build.py itself had none before this).
LEGAL_PAGES = ["terms", "returns", "privacy", "company"]
LEGAL_LANG_BLOCK_RE = re.compile(r":::\s*(lt|en|pl)\s*\n(.*?)\n:::", re.S)
_RAW_BLOCK_RE = re.compile(r"^\{\{(\w+)\}\}$|^<[a-zA-Z][^<>]*>(</[a-zA-Z][^<>]*>)?$")


def _inline_md(text: str) -> str:
    """Inline markdown: **bold** and [text](url) links, applied to
    already-HTML-escaped text (html.escape doesn't touch *, [, ], (, ) --
    so this ordering is safe: no double-escaping, and no raw HTML in the
    source markdown ever survives as anything but literal escaped text)."""
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)

    def _link(m):
        label, url = m.group(1), m.group(2)
        # Cross-links to the other legal pages are absolute (config.yaml's
        # terms_url/returns_url/etc.) but still same-site -- only open a
        # NEW tab for a genuinely external link (e.g. the ODR platform),
        # not for site-internal navigation.
        external = url.startswith("http") and not url.startswith(SITE_URL)
        target = ' target="_blank" rel="noopener"' if external else ""
        return f'<a href="{url}"{target}>{label}</a>'

    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, text)


def markdown_lite_to_html(md_text: str) -> str:
    """Deliberately minimal: headings (#/##), paragraphs, "- " lists,
    pipe tables, and two raw-passthrough escapes -- a lone {{placeholder}}
    line (for content that must inject real HTML post-conversion, e.g.
    privacy.md's processor table) or a lone simple HTML tag line (e.g.
    an <a id="..."></a> anchor) is emitted verbatim, unescaped, instead of
    being wrapped in <p> -- see terms.md's header comment for the full
    authoring syntax this supports."""
    blocks = re.split(r"\n\s*\n", md_text.strip())
    parts = []
    for block in blocks:
        lines = [l for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        stripped = lines[0].strip()
        if len(lines) == 1 and _RAW_BLOCK_RE.match(stripped):
            parts.append(stripped)
        elif stripped.startswith("### "):
            parts.append(f"<h3>{_inline_md(stripped[4:].strip())}</h3>")
        elif stripped.startswith("## "):
            parts.append(f"<h2>{_inline_md(stripped[3:].strip())}</h2>")
        elif stripped.startswith("# "):
            parts.append(f"<h1>{_inline_md(stripped[2:].strip())}</h1>")
        elif stripped.startswith("- "):
            # A list item may wrap onto a continuation line with no "- "
            # prefix (long return-terms/category descriptions do) -- only
            # a line that itself starts with "- " begins a NEW item;
            # anything else joins the current item's text.
            items, current = [], None
            for l in lines:
                l = l.strip()
                if l.startswith("- "):
                    if current is not None:
                        items.append(current)
                    current = l[2:]
                elif current is not None:
                    current += " " + l
            if current is not None:
                items.append(current)
            parts.append("<ul>" + "".join(f"<li>{_inline_md(it)}</li>" for it in items) + "</ul>")
        elif all(l.strip().startswith("|") for l in lines):
            rows = [[c.strip() for c in l.strip().strip("|").split("|")] for l in lines]
            header, sep, *body = rows
            thead = "<tr>" + "".join(f"<th>{_inline_md(c)}</th>" for c in header) + "</tr>"
            tbody = "".join("<tr>" + "".join(f"<td>{_inline_md(c)}</td>" for c in r) + "</tr>" for r in body)
            parts.append(f"<table>{thead}{tbody}</table>")
        else:
            parts.append(f"<p>{_inline_md(' '.join(l.strip() for l in lines))}</p>")
    return "\n".join(parts)


def parse_trilingual_markdown(md_text: str) -> dict:
    return {m.group(1): m.group(2) for m in LEGAL_LANG_BLOCK_RE.finditer(md_text)}


def _obfuscate_email(addr: str) -> str:
    """HTML numeric-character-reference encoding -- browsers render/decode
    it transparently (still a real, clickable mailto: link for a human
    visitor), but raw page source and simple regex/text scrapers see only
    entity codes, not a plain-text address. Only for the public legal
    pages (see legal_placeholders()) -- NOT for the actual order
    confirmation email (origo_common.py), which is a direct 1:1 message
    to a customer who already has the address, not a publicly scraped
    page."""
    return "".join(f"&#{ord(c)};" for c in addr)


# legal_placeholders() substitutes these sentinels in place of a real
# obfuscated email string -- _inline_md() runs html.escape() on link
# text/URLs AFTER substitution, which would otherwise turn our "&#111;"
# entities into "&amp;#111;" (visible garbage, not a decoded link). These
# sentinels contain no HTML-special characters, so they pass through
# escaping untouched; build_legal_pages() swaps them for the real
# obfuscated address in the final HTML, after escaping has already run.
INFO_EMAIL_SENTINEL = "\x00INFO_EMAIL_OBF\x00"
ORDERS_EMAIL_SENTINEL = "\x00ORDERS_EMAIL_OBF\x00"


def legal_placeholders(config: dict, lang: str) -> dict:
    business = config.get("business", {}) or {}
    address = (business.get("registered_address") or "").strip()
    owner = (business.get("owner_legal_name") or "").strip()
    owner_skipped = bool(business.get("owner_disclosure_skipped", False))
    reg_no = (business.get("registration_number") or "").strip()
    legal_name = (business.get("legal_name") or "").strip()

    address_tbd = {
        "lt": "[registruotas adresas – bus įrašytas]",
        "en": "[registered address — to be added]",
        "pl": "[adres siedziby — do uzupełnienia]",
    }[lang]
    owner_tbd = {
        "lt": "[savininko vardas, pavardė – nenurodyta]",
        "en": "[owner's full legal name — not yet set]",
        "pl": "[imię i nazwisko właściciela — nieuzupełnione]",
    }[lang]
    owner_line_prefix = {"lt": "savininkas", "en": "owner", "pl": "właściciel"}[lang]
    owner_row_label = {"lt": "Savininkas", "en": "Owner", "pl": "Właściciel"}[lang]
    reg_no_tbd = {"lt": "[nenurodytas]", "en": "[not yet set]", "pl": "[nieuzupełniony]"}[lang]
    legal_name_tbd = {
        "lt": "[registruotas pavadinimas – bus įrašytas]",
        "en": "[registered legal name — to be added]",
        "pl": "[nazwa zarejestrowana — do uzupełnienia]",
    }[lang]

    # owner_disclosure_skipped is a deliberate, permanent privacy choice
    # (distinct from "not yet filled in") -- omit the clause/row entirely
    # rather than showing a "[not yet set]" placeholder forever.
    if owner_skipped:
        owner_legal_name_line = ""
        owner_row = ""
    else:
        owner_display = owner or owner_tbd
        owner_legal_name_line = f"{owner_line_prefix} {owner_display}, " if owner else f"{owner_display}, "
        owner_row = f"- **{owner_row_label}:** {owner_display}\n"

    return {
        "business_name": business.get("name", "ORIGO"),
        "entity_type": business.get("entity_type", "IĮ"),
        "legal_name": legal_name or legal_name_tbd,
        "owner_legal_name_line": owner_legal_name_line,
        "owner_row": owner_row,
        "registration_number": reg_no or reg_no_tbd,
        "registered_address": address or address_tbd,
        "orders_email": (business.get("emails") or {}).get("orders", "orders@iieik.eu"),
        "info_email": (business.get("emails") or {}).get("info", "info@iieik.eu"),
        "orders_email_obf": ORDERS_EMAIL_SENTINEL,
        "info_email_obf": INFO_EMAIL_SENTINEL,
        "terms_url": business.get("terms_url", f"{SITE_URL}/terms.html"),
        "returns_url": business.get("returns_url", f"{SITE_URL}/returns.html"),
        "privacy_url": business.get("privacy_url", f"{SITE_URL}/privacy.html"),
        "company_url": business.get("company_url", f"{SITE_URL}/company.html"),
    }


def _substitute(text: str, values: dict) -> str:
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", str(val))
    return text


# Which config/accounts-registry.yaml entries are customer-data processors
# worth naming in privacy.md's table, vs. purely internal/ops accounts
# (NAS Gitea, Postiz) that never touch a customer's personal data. Hand-
# maintained allowlist -- keep in sync with accounts-registry.yaml by hand,
# same "edit both by hand" discipline as everywhere else legal text
# references config data in this codebase.
PROCESSOR_ALLOWLIST = ["Forward Email (forwardemail.net)",
                       "Gmail — iieik.web@gmail.com (dedicated ORIGO mailbox)",
                       "Resend — ORIGO-dedicated account",
                       "Cloudflare", "GitHub — github.com/fuence/origo",
                       "Bank — SEPA payout account", "LP Express", "Omniva", "DPD"]
PROCESSOR_ROLE = {
    "lt": {
        "Forward Email (forwardemail.net)": "Gaunamų el. laiškų persiuntimas",
        "Gmail — iieik.web@gmail.com (dedicated ORIGO mailbox)": "El. pašto dėžutė",
        "Resend — ORIGO-dedicated account": "Išsiunčiamų el. laiškų siuntimas (ES/Airijos regionas)",
        "Cloudflare": "DNS / interneto svetainės maršrutizavimas",
        "GitHub — github.com/fuence/origo": "Viešos svetainės talpinimas",
        "Bank — SEPA payout account": "Mokėjimų priėmimas (banko pavedimas)",
        "LP Express": "Pristatymas", "Omniva": "Pristatymas", "DPD": "Pristatymas",
    },
    "en": {
        "Forward Email (forwardemail.net)": "Inbound email forwarding",
        "Gmail — iieik.web@gmail.com (dedicated ORIGO mailbox)": "Email mailbox",
        "Resend — ORIGO-dedicated account": "Outbound email sending (EU/Ireland region)",
        "Cloudflare": "DNS / site routing",
        "GitHub — github.com/fuence/origo": "Public site hosting",
        "Bank — SEPA payout account": "Payment receipt (bank transfer)",
        "LP Express": "Delivery", "Omniva": "Delivery", "DPD": "Delivery",
    },
    "pl": {
        "Forward Email (forwardemail.net)": "Przekazywanie poczty przychodzącej",
        "Gmail — iieik.web@gmail.com (dedicated ORIGO mailbox)": "Skrzynka pocztowa",
        "Resend — ORIGO-dedicated account": "Wysyłka poczty wychodzącej (region UE/Irlandia)",
        "Cloudflare": "DNS / routing strony",
        "GitHub — github.com/fuence/origo": "Hosting publicznej strony",
        "Bank — SEPA payout account": "Przyjmowanie płatności (przelew bankowy)",
        "LP Express": "Dostawa", "Omniva": "Dostawa", "DPD": "Dostawa",
    },
}
PROCESSOR_TABLE_HEAD = {
    "lt": ("Gavėjas", "Paskirtis", "DPA / duomenų tvarkymo sąlygos"),
    "en": ("Recipient", "Purpose", "DPA / data terms"),
    "pl": ("Odbiorca", "Cel", "DPA / warunki przetwarzania"),
}
# Link label shown for a dpa_note_url (NOT a real DPA -- see the Gmail
# entry's comment in accounts-registry.yaml for why one isn't cited).
PROCESSOR_DPA_NOTE_LABEL = {
    "lt": "Privatumo politika (ne DPA)",
    "en": "Privacy Policy (not a DPA)",
    "pl": "Polityka prywatności (nie DPA)",
}
PROCESSOR_DPA_LABEL = {"lt": "DPA", "en": "DPA", "pl": "DPA"}


def legal_processor_table(lang: str) -> str:
    if not ACCOUNTS_REGISTRY_PATH.is_file():
        return ""
    data = yaml.safe_load(ACCOUNTS_REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    accounts = {a.get("service"): a for a in data.get("accounts", [])}
    head_recipient, head_purpose, head_dpa = PROCESSOR_TABLE_HEAD[lang]
    rows = []
    for service in PROCESSOR_ALLOWLIST:
        if service not in accounts:
            continue
        role = PROCESSOR_ROLE[lang].get(service, "")
        account = accounts[service]
        if account.get("dpa_url"):
            dpa_cell = f'<a href="{html.escape(account["dpa_url"])}">{html.escape(PROCESSOR_DPA_LABEL[lang])}</a>'
        elif account.get("dpa_note_url"):
            dpa_cell = f'<a href="{html.escape(account["dpa_note_url"])}">{html.escape(PROCESSOR_DPA_NOTE_LABEL[lang])}</a>'
        else:
            dpa_cell = "—"
        rows.append(
            f"<tr><td>{html.escape(service)}</td><td>{html.escape(role)}</td><td>{dpa_cell}</td></tr>"
        )
    return (
        f"<table><tr><th>{html.escape(head_recipient)}</th><th>{html.escape(head_purpose)}</th>"
        f"<th>{html.escape(head_dpa)}</th></tr>"
        + "".join(rows) + "</table>"
    )


def _seller_block_text(config: dict, lang: str) -> str:
    """Mirrors origo_common.build_seller_block() (used for the SAME
    withdrawal-form text in the confirmation email) -- labels from
    config/legal-texts/seller-identification.yaml, values from
    legal_placeholders(). Kept in sync with that function's field order
    by hand; the two can't share code directly since this repo (origo-
    site) and origo_common.py (futura-genesis) are separate projects.
    One deliberate difference: this version points to the company.html
    page instead of repeating a literal email address (Elijo's call,
    2026-07-24) -- the confirmation-email version keeps the real address
    inline since it's a direct 1:1 message the customer already has, not
    a publicly scraped page."""
    path = LEGAL_TEXTS_DIR / "seller-identification.yaml"
    labels = {}
    if path.is_file():
        labels = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(lang, {})
    values = legal_placeholders(config, lang)
    lines = []
    if labels.get("heading"):
        lines.append(f"{labels['heading']}:")
    lines.append(f"{labels.get('name_label', 'Company')}: {values['business_name']}")
    if values["entity_type"]:
        lines.append(f"{labels.get('entity_label', 'Legal form')}: {values['entity_type']}")
    lines.append(f"{labels.get('address_label', 'Address')}: {values['registered_address']}")
    # Email deliberately NOT repeated here as a literal address -- Elijo's
    # call (2026-07-24): mention the real address once (company.html),
    # everywhere else just point back to it.
    contact_note = {
        "lt": f"{labels.get('email_label', 'El. paštas')}: žr. {values['company_url']}",
        "en": f"{labels.get('email_label', 'Email')}: see {values['company_url']}",
        "pl": f"{labels.get('email_label', 'E-mail')}: zob. {values['company_url']}",
    }[lang]
    lines.append(contact_note)
    return "\n".join(lines)


def withdrawal_form_html(config: dict, lang: str) -> str:
    """Appends the EU Model Withdrawal Form (Directive 2011/83/EU, Annex
    I(B)) to the end of returns.html's #withdrawal-form section, reusing
    config/legal-texts/withdrawal-form.yaml's own trilingual text (the
    SAME source origo_common.build_withdrawal_form_text() uses for the
    confirmation email) so the Directive's exact wording lives in exactly
    one file, not duplicated into returns.md's prose. {{seller_block}} is
    substituted the same way that function does it -- on the raw text,
    before splitting into display lines."""
    path = LEGAL_TEXTS_DIR / "withdrawal-form.yaml"
    if not path.is_file():
        return ""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    text = (data.get(lang) or "").strip()
    if not text:
        return ""
    text = text.replace("{{seller_block}}", _seller_block_text(config, lang))
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    body = "".join(f"<p>{html.escape(l)}</p>" for l in lines)
    return f'<div class="form-box">{body}</div>'


PAGE_TITLES = {
    "terms": {"lt": "Pirkimo–pardavimo taisyklės", "en": "Terms & Conditions", "pl": "Regulamin"},
    "returns": {"lt": "Grąžinimo sąlygos", "en": "Returns Policy", "pl": "Polityka zwrotów"},
    "privacy": {"lt": "Privatumo politika", "en": "Privacy Policy", "pl": "Polityka prywatności"},
    "company": {"lt": "Apie pardavėją", "en": "About the seller", "pl": "O sprzedawcy"},
}
DRAFT_NOTICE = {
    "lt": "⚠ JUODRAŠTIS — laukiama Elijo ir buhalterio/teisininko peržiūros prieš paskelbimą. "
          "Žr. REVIEW-CHECKLIST.md (origo-ops repo).",
    "en": "⚠ DRAFT — pending Elijo's and his accountant's/lawyer's review before go-live. "
          "See REVIEW-CHECKLIST.md (origo-ops repo).",
    "pl": "⚠ WERSJA ROBOCZA — oczekuje na przegląd Elijo oraz księgowego/prawnika przed publikacją. "
          "Zobacz REVIEW-CHECKLIST.md (repozytorium origo-ops).",
}
LEGAL_PAGE_CSS = """
:root{--ink:#1a1a1a;--cream:#f5f0e8;--olive:#3d5a3e;--olive-l:#5a7c5b;--gold:#b8860b;--warm:#f9f5ee;--muted:#6b6560;--border:#d9d2c4;--white:#ffffff}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'DM Sans',sans-serif;background:var(--warm);color:var(--ink);line-height:1.7;font-size:16px}
.lang-bar{background:var(--olive);padding:6px 0;text-align:center}
.lang-bar a{color:rgba(255,255,255,.6);text-decoration:none;font-size:12px;letter-spacing:.1em;text-transform:uppercase;margin:0 10px}
.lang-bar a.active{color:#fff;font-weight:500;text-decoration:underline;text-underline-offset:3px}
header{background:var(--cream);border-bottom:1px solid var(--border);padding:20px 24px}
header a{font-family:'Cormorant Garamond',serif;font-size:26px;color:var(--olive);text-decoration:none;letter-spacing:.1em}
.wrap{max-width:760px;margin:0 auto;padding:48px 24px 80px}
h1{font-family:'Cormorant Garamond',serif;font-size:clamp(32px,5vw,44px);font-weight:400;margin-bottom:8px}
h2{font-family:'Cormorant Garamond',serif;font-size:24px;font-weight:600;margin:36px 0 12px;color:var(--olive)}
h3{font-size:16px;font-weight:600;margin:20px 0 8px;color:var(--olive)}
p,li{font-size:14.5px;color:#3a3733}
p{margin-bottom:12px}
ul,ol{margin:0 0 12px 22px}
li{margin-bottom:6px}
a{color:var(--olive)}
table{width:100%;border-collapse:collapse;margin:12px 0;font-size:13.5px}
td,th{padding:8px 10px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}
th{color:var(--olive);font-weight:600}
.draft-notice{background:#fff4e5;border-left:3px solid var(--gold);padding:14px 18px;border-radius:6px;font-size:13px;color:#6b4f00;margin:0 0 24px;font-weight:600}
.form-box{background:var(--white);border:1px solid var(--border);border-radius:10px;padding:20px 24px;margin-top:16px}
.form-box p{font-size:13.5px;margin-bottom:10px}
footer{text-align:center;padding:32px 24px;font-size:12px;color:var(--muted);border-top:1px solid var(--border)}
[data-lang]{display:none}
body.lt [data-lang="lt"]{display:block}
body.en [data-lang="en"]{display:block}
body.pl [data-lang="pl"]{display:block}
"""


def render_legal_page(page_id: str, body_html_by_lang: dict, reviewed: bool) -> str:
    titles = PAGE_TITLES[page_id]
    title_line = f"{titles['lt']} | {titles['en']} | {titles['pl']}"
    lang_divs = []
    for lang in ("lt", "en", "pl"):
        notice = "" if reviewed else f'<div class="draft-notice">{DRAFT_NOTICE[lang]}</div>'
        lang_divs.append(f'<div data-lang="{lang}">{notice}{body_html_by_lang.get(lang, "")}</div>')
    return f"""<!DOCTYPE html>
<html lang="lt">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ORIGO — {html.escape(title_line)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>{LEGAL_PAGE_CSS}</style>
</head>
<body class="lt">
<div class="lang-bar">
  <a href="#" onclick="setLang('lt');return false" id="lang-lt" class="active">LT</a>
  <a href="#" onclick="setLang('en');return false" id="lang-en">EN</a>
  <a href="#" onclick="setLang('pl');return false" id="lang-pl">PL</a>
</div>
<header><a href="index.html">ORIGO</a></header>
<div class="wrap">
{"".join(lang_divs)}
</div>
<footer>© 2026 ORIGO / iieik.eu · <a href="index.html">iieik.eu</a></footer>
<script>
function setLang(lang){{
  document.body.className = lang;
  ['lt','en','pl'].forEach(l => document.getElementById('lang-'+l).className = l===lang ? 'active' : '');
  localStorage.setItem('origo-lang', lang);
}}
document.addEventListener('DOMContentLoaded', () => setLang(localStorage.getItem('origo-lang') || 'lt'));
</script>
</body>
</html>
"""


def build_legal_pages(config: dict) -> None:
    """Hard build failure if any of the 4 .md sources is missing -- a
    controllable, one-time condition (unlike the food_info gate, which
    stays soft/per-product, see this module's docstring)."""
    missing = [p for p in LEGAL_PAGES if not (LEGAL_TEXTS_DIR / f"{p}.md").is_file()]
    if missing:
        sys.exit(
            "ERROR: missing legal-text source(s): "
            + ", ".join(f"{LEGAL_TEXTS_DIR / (p + '.md')}" for p in missing)
            + "\nEvery one of terms.md/returns.md/privacy.md/company.md must exist "
            "before the site can build — see config/legal-texts/ (origo-ops repo)."
        )

    reviewed = bool(config.get("legal_texts_reviewed", False))
    business = config.get("business", {}) or {}
    obf_info_email = _obfuscate_email((business.get("emails") or {}).get("info", "info@iieik.eu"))
    obf_orders_email = _obfuscate_email((business.get("emails") or {}).get("orders", "orders@iieik.eu"))
    processor_tables = {lang: legal_processor_table(lang) for lang in ("lt", "en", "pl")}
    withdrawal_forms = {lang: withdrawal_form_html(config, lang) for lang in ("lt", "en", "pl")}

    for page_id in LEGAL_PAGES:
        md_text = (LEGAL_TEXTS_DIR / f"{page_id}.md").read_text(encoding="utf-8")
        lang_blocks = parse_trilingual_markdown(md_text)
        body_html_by_lang = {}
        for lang in ("lt", "en", "pl"):
            raw = lang_blocks.get(lang, "")
            values = legal_placeholders(config, lang)
            raw = _substitute(raw, values)
            body_html = markdown_lite_to_html(raw)
            body_html = body_html.replace(INFO_EMAIL_SENTINEL, obf_info_email)
            body_html = body_html.replace(ORDERS_EMAIL_SENTINEL, obf_orders_email)
            body_html = body_html.replace("{{processor_table}}", processor_tables[lang])
            if page_id == "returns" and withdrawal_forms[lang]:
                body_html += withdrawal_forms[lang]
            body_html_by_lang[lang] = body_html
        page_path = ROOT / f"{page_id}.html"
        page_path.write_text(render_legal_page(page_id, body_html_by_lang, reviewed), encoding="utf-8")
        print(f"wrote {page_path}")


def main():
    products = load_products()
    config = load_config()

    # Track 2 withdrawal/guarantee/condition consistency, fail fast --
    # see validate_track2_terms()'s docstring.
    validate_track2_terms(products)

    # Legal pages first, fail fast -- see build_legal_pages()'s docstring
    # for why this is a hard failure (unlike food_info below).
    build_legal_pages(config)

    # Single-source policy (Segment P1) -- warn (or block, per config) on
    # live SKUs with fewer than 2 suppliers at stage >= quoted.
    suppliers = load_suppliers()
    single_source_warnings = check_single_source_policy(products, suppliers, config)
    if single_source_warnings:
        print(f"\n*** SINGLE-SOURCE POLICY WARNING — {len(single_source_warnings)} live SKU(s): ***", file=sys.stderr)
        for w in single_source_warnings:
            print(f"  - {w}", file=sys.stderr)
        print("", file=sys.stderr)

    products_json_path = ROOT / "products.json"
    products_json_data, gaps_report = build_products_json(products, config)
    products_json_path.write_text(
        json.dumps(products_json_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {products_json_path}")

    excluded_skus = set(gaps_report.keys())
    if excluded_skus:
        print(
            f"\n*** FOOD_INFO INCOMPLETE — {len(excluded_skus)} live product(s) excluded "
            f"from products.json/feed.xml/paste-packs (EU Reg 1169/2011 gate): ***",
            file=sys.stderr,
        )
        for sku, gaps in gaps_report.items():
            print(f"  {sku}:", file=sys.stderr)
            for gap in gaps:
                print(f"    - {gap}", file=sys.stderr)
        FOOD_INFO_GAPS_PATH.parent.mkdir(parents=True, exist_ok=True)
        FOOD_INFO_GAPS_PATH.write_text(
            json.dumps({
                "generated_at": products_json_data["generated_at"],
                "gaps": gaps_report,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"*** full report: {FOOD_INFO_GAPS_PATH} ***\n", file=sys.stderr)
    elif FOOD_INFO_GAPS_PATH.is_file():
        FOOD_INFO_GAPS_PATH.unlink()

    # Procurement blockers (Segment P1) -- bridges the food_info gate above
    # with sourcing/RFQ status. Always (re)written, even when gaps_report is
    # empty (an empty {} is itself meaningful -- "no blockers right now").
    blockers = build_procurement_blockers(gaps_report, suppliers)
    PROCUREMENT_BLOCKERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROCUREMENT_BLOCKERS_PATH.write_text(
        json.dumps({"generated_at": products_json_data["generated_at"], "blockers": blockers}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {PROCUREMENT_BLOCKERS_PATH}")

    feed_xml_path = ROOT / "feed.xml"
    feed_xml_text, feed_skus = build_feed_xml(products, excluded_skus)
    feed_xml_path.write_text(feed_xml_text, encoding="utf-8")
    print(f"wrote {feed_xml_path}")

    paste_packs_dir = ROOT / "dispatch" / "paste-packs"
    paste_packs_dir.mkdir(parents=True, exist_ok=True)
    existing = {f for f in paste_packs_dir.glob("*-skelbimai.txt")}
    written = set()
    paste_pack_skus = set()
    for p in live_products(products):
        if p["sku"] in excluded_skus:
            continue
        if not p.get("platforms", {}).get("skelbimai", True):
            continue
        out_path = paste_packs_dir / f"{p['sku']}-skelbimai.txt"
        out_path.write_text(build_paste_pack(p), encoding="utf-8")
        written.add(out_path)
        paste_pack_skus.add(p["sku"])
        print(f"wrote {out_path}")
    # Clean up paste-packs for products no longer live/skelbimai-listed, so
    # the folder never silently drifts from products.yaml's current state.
    for stale in existing - written:
        stale.unlink()
        print(f"removed stale {stale}")

    write_site_outputs_mirror(products_json_data, feed_skus, paste_pack_skus)
    print(f"wrote {SITE_MIRROR_PATH}")


if __name__ == "__main__":
    main()
