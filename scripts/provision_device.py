#!/usr/bin/env python3
"""Admin CLI for managing devices in Firestore.

Usage:
    provision_device.py register DEVICE_ID --pubkey path/to/device.pub.pem [--site SITE]
    provision_device.py set-state DEVICE_ID STATE [--confirm]
    provision_device.py request-wipe DEVICE_ID --confirm
    provision_device.py revoke DEVICE_ID [--reason "lost device"]   # alias of set-state revoked
    provision_device.py unrevoke DEVICE_ID --confirm                 # alias of set-state active
    provision_device.py info DEVICE_ID
    provision_device.py list [--site SITE] [--state STATE] [--include-terminal]

Requires:
    pip install -r scripts/requirements.txt
    gcloud auth application-default login
    export GOOGLE_CLOUD_PROJECT=<your-project>
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import click
from cryptography.hazmat.primitives import serialization
from google.cloud import firestore


# ---------------------------------------------------------------------------
# State machine — mirrors backend/gateway/src/gateway/state_machine.py.
# Vendored here so this script has no path dependency on the services.
# ---------------------------------------------------------------------------

STATE_ACTIVE = "active"
STATE_LOST = "lost"
STATE_REVOKED = "revoked"
STATE_WIPE_REQUESTED = "wipe_requested"
STATE_WIPE_SENT = "wipe_sent"

ALL_STATES = {
    STATE_ACTIVE,
    STATE_LOST,
    STATE_REVOKED,
    STATE_WIPE_REQUESTED,
    STATE_WIPE_SENT,
}

_ADMIN_TRANSITIONS: dict[str, set[str]] = {
    STATE_ACTIVE: {STATE_LOST, STATE_REVOKED, STATE_WIPE_REQUESTED},
    STATE_LOST: {STATE_ACTIVE, STATE_REVOKED, STATE_WIPE_REQUESTED},
    STATE_REVOKED: {STATE_ACTIVE},
    STATE_WIPE_REQUESTED: set(),
    STATE_WIPE_SENT: set(),
}

EXTRA_CONFIRM = {
    (STATE_REVOKED, STATE_ACTIVE),
    (STATE_ACTIVE, STATE_WIPE_REQUESTED),
    (STATE_LOST, STATE_WIPE_REQUESTED),
}


def _normalize_state(state: str | None) -> str:
    if not state:
        return STATE_ACTIVE
    s = state.strip().lower()
    if s in ALL_STATES:
        return s
    if s == "offline":
        return STATE_ACTIVE
    return STATE_ACTIVE


def _validate_transition(current: str, target: str) -> None:
    cur = _normalize_state(current)
    tgt = _normalize_state(target)
    if tgt not in ALL_STATES:
        raise click.UsageError(f"unknown target state: {target!r}")
    if tgt == STATE_WIPE_SENT:
        raise click.UsageError("wipe_sent is gateway-internal; cannot be set via CLI")
    if tgt not in _ADMIN_TRANSITIONS.get(cur, set()):
        raise click.UsageError(f"transition not allowed: {cur} -> {tgt}")


def _client(project: str | None) -> firestore.Client:
    return firestore.Client(project=project or os.environ.get("GOOGLE_CLOUD_PROJECT"))


def _validate_public_key(pem_text: str) -> str:
    pem_bytes = pem_text.encode()
    pub = serialization.load_pem_public_key(pem_bytes)
    return pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


@click.group()
@click.option("--project", help="GCP project id (defaults to GOOGLE_CLOUD_PROJECT)")
@click.option("--collection", default="devices", show_default=True)
@click.pass_context
def cli(ctx: click.Context, project: str | None, collection: str) -> None:
    ctx.ensure_object(dict)
    ctx.obj["client"] = _client(project)
    ctx.obj["collection"] = collection


@cli.command()
@click.argument("device_id")
@click.option(
    "--pubkey",
    "pubkey_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to PEM-encoded EC P-256 public key exported from the device.",
)
@click.option("--site", default="", help="Optional site/location label.")
@click.option("--notes", default="", help="Free-form admin notes.")
@click.pass_context
def register(
    ctx: click.Context,
    device_id: str,
    pubkey_path: Path,
    site: str,
    notes: str,
) -> None:
    """Register or update a device with its hardware-backed public key."""
    pem = _validate_public_key(pubkey_path.read_text())
    now = int(time.time() * 1000)

    db = ctx.obj["client"]
    doc = db.collection(ctx.obj["collection"]).document(device_id)
    snap = doc.get()
    payload: dict[str, Any] = {
        "device_id": device_id,
        "public_key_pem": pem,
        "assigned_site_label": site,
        "admin_notes": notes,
        "registered_at_ms": now,
    }
    if not snap.exists:
        payload["first_seen_ms"] = now
        payload["state"] = STATE_ACTIVE
    doc.set(payload, merge=True)
    click.echo(f"Registered {device_id} (site={site or '-'})")


@cli.command(name="set-state")
@click.argument("device_id")
@click.argument("target_state")
@click.option(
    "--confirm",
    is_flag=True,
    help="Required for transitions that need an extra confirmation "
    "(revoked -> active, * -> wipe_requested).",
)
@click.pass_context
def set_state(
    ctx: click.Context,
    device_id: str,
    target_state: str,
    confirm: bool,
) -> None:
    """Move a device into a new lifecycle state."""
    target = _normalize_state(target_state)
    db = ctx.obj["client"]
    doc_ref = db.collection(ctx.obj["collection"]).document(device_id)

    @firestore.transactional
    def _txn(txn: firestore.Transaction) -> tuple[str, str]:
        snap = doc_ref.get(transaction=txn)
        if not snap.exists:
            raise click.UsageError(f"unknown device: {device_id}")
        data = snap.to_dict() or {}
        current = _normalize_state(data.get("state") or data.get("status"))
        _validate_transition(current, target)
        if (current, target) in EXTRA_CONFIRM and not confirm:
            raise click.UsageError(
                f"transition {current} -> {target} requires --confirm"
            )
        now_ms = int(time.time() * 1000)
        update: dict[str, Any] = {"state": target}
        if target == STATE_WIPE_REQUESTED:
            update["wipe_requested_at_ms"] = now_ms
            update["last_wipe_request_admin"] = os.environ.get("USER", "unknown")
        if target == STATE_ACTIVE:
            update["wipe_requested_at_ms"] = None
        txn.set(doc_ref, update, merge=True)
        return current, target

    txn = db.transaction()
    current, target = _txn(txn)
    click.echo(f"{device_id}: {current} -> {target}")


@cli.command(name="request-wipe")
@click.argument("device_id")
@click.option(
    "--confirm",
    required=True,
    help='Pass --confirm WIPE to acknowledge the irreversible nature of this action.',
)
@click.pass_context
def request_wipe(ctx: click.Context, device_id: str, confirm: str) -> None:
    """Queue a remote wipe on the next time the device connects."""
    if confirm != "WIPE":
        raise click.UsageError('pass --confirm WIPE to acknowledge')
    ctx.invoke(set_state, device_id=device_id, target_state=STATE_WIPE_REQUESTED, confirm=True)


@cli.command()
@click.argument("device_id")
@click.option("--reason", default="manual revoke", help="Stored on the device doc.")
@click.pass_context
def revoke(ctx: click.Context, device_id: str, reason: str) -> None:
    """Set state=revoked. Alias of `set-state DEVICE_ID revoked`."""
    db = ctx.obj["client"]
    doc_ref = db.collection(ctx.obj["collection"]).document(device_id)
    if not doc_ref.get().exists:
        raise click.UsageError(f"unknown device: {device_id}")
    ctx.invoke(set_state, device_id=device_id, target_state=STATE_REVOKED, confirm=False)
    doc_ref.set(
        {"last_revocation_reason": reason, "revoked_at_ms": int(time.time() * 1000)},
        merge=True,
    )


@cli.command()
@click.argument("device_id")
@click.option(
    "--confirm",
    is_flag=True,
    help="Required to flip a revoked device back to active.",
)
@click.pass_context
def unrevoke(ctx: click.Context, device_id: str, confirm: bool) -> None:
    """Restore a revoked device to active. Alias of `set-state DEVICE_ID active --confirm`."""
    ctx.invoke(set_state, device_id=device_id, target_state=STATE_ACTIVE, confirm=confirm)


@cli.command()
@click.argument("device_id")
@click.pass_context
def info(ctx: click.Context, device_id: str) -> None:
    """Print everything Firestore knows about a device."""
    db = ctx.obj["client"]
    snap = db.collection(ctx.obj["collection"]).document(device_id).get()
    if not snap.exists:
        click.echo(f"No such device: {device_id}", err=True)
        sys.exit(1)
    data = snap.to_dict() or {}
    if "public_key_pem" in data and isinstance(data["public_key_pem"], str):
        pem = data["public_key_pem"]
        data["public_key_pem"] = (
            pem.splitlines()[0] + f" ... (truncated; full length: {len(pem)})"
        )
    if "current_location" in data and data["current_location"] is not None:
        loc = data["current_location"]
        data["current_location"] = {
            "latitude": getattr(loc, "latitude", None),
            "longitude": getattr(loc, "longitude", None),
        }
    data["state"] = _normalize_state(data.get("state") or data.get("status"))
    click.echo(json.dumps(data, indent=2, default=str))


@cli.command(name="list")
@click.option("--site", default="", help="Filter by site label.")
@click.option(
    "--state",
    default="",
    help="Filter to a specific state (active, lost, revoked, wipe_requested, wipe_sent).",
)
@click.option(
    "--include-terminal/--no-include-terminal",
    default=False,
    help="Show wipe_sent devices in the list.",
)
@click.pass_context
def list_cmd(ctx: click.Context, site: str, state: str, include_terminal: bool) -> None:
    """List registered devices."""
    db = ctx.obj["client"]
    query = db.collection(ctx.obj["collection"])
    if site:
        query = query.where("assigned_site_label", "==", site)
    target_state = _normalize_state(state) if state else None
    rows = []
    for snap in query.stream():
        data = snap.to_dict() or {}
        device_state = _normalize_state(data.get("state") or data.get("status"))
        if target_state and device_state != target_state:
            continue
        if not include_terminal and device_state == STATE_WIPE_SENT:
            continue
        rows.append(
            (
                snap.id,
                device_state,
                data.get("assigned_site_label") or "-",
                data.get("app_version") or "-",
                data.get("last_seen_ms") or "",
            )
        )
    rows.sort(key=lambda r: r[0])
    click.echo(f"{'device_id':<24}  {'state':<16}  {'site':<16}  {'app':<10}  last_seen_ms")
    click.echo("-" * 90)
    for r in rows:
        click.echo(f"{r[0]:<24}  {r[1]:<16}  {r[2]:<16}  {r[3]:<10}  {r[4]}")


if __name__ == "__main__":
    cli()
