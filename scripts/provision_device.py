#!/usr/bin/env python3
"""Admin CLI for registering / revoking devices in Firestore.

Usage:
    provision_device.py register DEVICE_ID --pubkey path/to/device.pub.pem [--site SITE]
    provision_device.py revoke DEVICE_ID [--reason "lost device"]
    provision_device.py unrevoke DEVICE_ID
    provision_device.py info DEVICE_ID
    provision_device.py list [--site SITE]

Requires:
    pip install google-cloud-firestore cryptography click
    gcloud auth application-default login
    export GOOGLE_CLOUD_PROJECT=<your-project>
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import click
from cryptography.hazmat.primitives import serialization
from google.cloud import firestore


def _client(project: str | None) -> firestore.Client:
    return firestore.Client(project=project or os.environ.get("GOOGLE_CLOUD_PROJECT"))


def _validate_public_key(pem_text: str) -> str:
    pem_bytes = pem_text.encode()
    pub = serialization.load_pem_public_key(pem_bytes)
    # Re-serialize to canonical PEM so it's always normalized on disk.
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
    payload = {
        "device_id": device_id,
        "public_key_pem": pem,
        "status": "active",
        "assigned_site_label": site,
        "admin_notes": notes,
        "registered_at_ms": now,
        "last_revocation_reason": None,
    }
    if not snap.exists:
        payload["first_seen_ms"] = now
    doc.set(payload, merge=True)
    click.echo(f"Registered {device_id} (site={site or '-'})")


@cli.command()
@click.argument("device_id")
@click.option("--reason", default="manual revoke", help="Why the device was revoked.")
@click.pass_context
def revoke(ctx: click.Context, device_id: str, reason: str) -> None:
    """Mark a device as revoked. Gateway will refuse its JWTs."""
    db = ctx.obj["client"]
    doc = db.collection(ctx.obj["collection"]).document(device_id)
    if not doc.get().exists:
        click.echo(f"ERROR: device {device_id} not registered", err=True)
        sys.exit(1)
    doc.set(
        {
            "status": "revoked",
            "last_revocation_reason": reason,
            "revoked_at_ms": int(time.time() * 1000),
        },
        merge=True,
    )
    click.echo(f"Revoked {device_id}: {reason}")


@cli.command()
@click.argument("device_id")
@click.pass_context
def unrevoke(ctx: click.Context, device_id: str) -> None:
    """Restore a previously-revoked device."""
    db = ctx.obj["client"]
    doc = db.collection(ctx.obj["collection"]).document(device_id)
    if not doc.get().exists:
        click.echo(f"ERROR: device {device_id} not registered", err=True)
        sys.exit(1)
    doc.set(
        {"status": "active", "last_revocation_reason": None, "revoked_at_ms": None},
        merge=True,
    )
    click.echo(f"Unrevoked {device_id}")


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
    # Truncate the public key for readability.
    if "public_key_pem" in data and isinstance(data["public_key_pem"], str):
        pem = data["public_key_pem"]
        data["public_key_pem"] = (
            pem.splitlines()[0] + " ... (truncated; full length: %d)" % len(pem)
        )
    if "current_location" in data:
        loc = data["current_location"]
        data["current_location"] = {
            "latitude": getattr(loc, "latitude", None),
            "longitude": getattr(loc, "longitude", None),
        }
    click.echo(json.dumps(data, indent=2, default=str))


@cli.command(name="list")
@click.option("--site", default="", help="Filter by site label.")
@click.option("--include-revoked/--no-include-revoked", default=False)
@click.pass_context
def list_cmd(ctx: click.Context, site: str, include_revoked: bool) -> None:
    """List registered devices."""
    db = ctx.obj["client"]
    query = db.collection(ctx.obj["collection"])
    if site:
        query = query.where("assigned_site_label", "==", site)
    rows = []
    for snap in query.stream():
        data = snap.to_dict() or {}
        status = data.get("status", "?")
        if not include_revoked and status == "revoked":
            continue
        rows.append(
            (
                snap.id,
                status,
                data.get("assigned_site_label") or "-",
                data.get("app_version") or "-",
                data.get("last_seen_ms") or "",
            )
        )
    rows.sort(key=lambda r: r[0])
    click.echo(f"{'device_id':<24}  {'status':<8}  {'site':<16}  {'app':<10}  last_seen_ms")
    click.echo("-" * 80)
    for r in rows:
        click.echo(f"{r[0]:<24}  {r[1]:<8}  {r[2]:<16}  {r[3]:<10}  {r[4]}")


if __name__ == "__main__":
    cli()
