#!/usr/bin/env python3
"""Mint a TEST-ONLY CA + per-station credentials for the Argos UAT
replay simulator and store them in argosuat Secret Manager.

Nothing this script creates represents any real PKI material. The CA
is self-signed, the per-station keys are freshly generated, and every
artifact is labelled "TEST" in the CN and the secret name so it can't
be confused with prod credentials.

Outputs (Secret Manager in project argosuat):
  drone-sensor-dev-test-ca-cert        PEM-encoded self-signed test CA cert
  drone-sensor-dev-test-ca-key         PEM-encoded test CA private key
  drone-sensor-dev-sim-cert-<STATION>  PEM-encoded station cert (signed by CA)
  drone-sensor-dev-sim-key-<STATION>   PEM-encoded station private key

Also writes the public-key PEM for every station to a local file at
``out_pubkeys/<STATION>.pub.pem`` so enroll_stations.py can pick them
up without re-fetching from Secret Manager.

Run-once. Safe to re-run: each call mints new material and adds a new
Secret Manager version. Old versions remain accessible.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from google.cloud import secretmanager

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stations import STATIONS

log = logging.getLogger(__name__)

CA_CN = "ARGOS UAT TEST CA"
CA_VALIDITY_DAYS = 365
LEAF_VALIDITY_DAYS = 90
KEY_SIZE = 2048


def _now_utc() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


def _generate_keypair() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)


def mint_ca() -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    """Generate a self-signed test CA. The cert serial uses the current
    timestamp so re-runs don't collide; this is sandbox material so
    revocation policy is "delete the secret."""
    key = _generate_keypair()
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, CA_CN),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "drone-sensor-dev (sandbox)"),
    ])
    now = _now_utc()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(int(now.timestamp()))
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=CA_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def mint_leaf(
    *, station_id: str, ca_key: rsa.RSAPrivateKey, ca_cert: x509.Certificate
) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = _generate_keypair()
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"{station_id} (TEST)"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "drone-sensor-dev (sandbox)"),
    ])
    now = _now_utc()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        # serial: high-bit-clear int from now + station hash so concurrent
        # mints don't collide.
        .serial_number(int(now.timestamp()) * 1000 + (hash(station_id) & 0x3FF))
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=LEAF_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(station_id)]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def _key_pem(key: rsa.RSAPrivateKey) -> bytes:
    # Unencrypted PEM. The encryption-at-rest is provided by Secret
    # Manager (Google-managed key); a passphrase here would only block
    # automated startup on the bridge VM with no real security gain in
    # a sandbox tier.
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _cert_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(encoding=serialization.Encoding.PEM)


def _pubkey_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _ensure_secret(client, project_id: str, name: str) -> str:
    """Create the secret container if missing. Returns the full path."""
    parent = f"projects/{project_id}"
    secret_path = f"{parent}/secrets/{name}"
    try:
        client.get_secret(request={"name": secret_path})
    except Exception:
        client.create_secret(
            request={
                "parent": parent,
                "secret_id": name,
                "secret": {"replication": {"automatic": {}}},
            }
        )
    return secret_path


def _add_version(client, secret_path: str, payload: bytes) -> None:
    client.add_secret_version(
        request={"parent": secret_path, "payload": {"data": payload}}
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project", default="argosuat", help="GCP project (Secret Manager target)"
    )
    parser.add_argument(
        "--out-pubkeys",
        default=str(Path(__file__).resolve().parent / "out_pubkeys"),
        help="Directory to write per-station public keys (for enroll_stations.py)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    out_dir = Path(args.out_pubkeys)
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("minting test CA")
    ca_key, ca_cert = mint_ca()

    sm = secretmanager.SecretManagerServiceClient()

    ca_cert_secret = _ensure_secret(sm, args.project, "drone-sensor-dev-test-ca-cert")
    ca_key_secret = _ensure_secret(sm, args.project, "drone-sensor-dev-test-ca-key")
    _add_version(sm, ca_cert_secret, _cert_pem(ca_cert))
    _add_version(sm, ca_key_secret, _key_pem(ca_key))
    log.info("ca uploaded -> %s, %s", ca_cert_secret, ca_key_secret)

    # Secret + pubkey filenames key on the short Argos id (SH011), not
    # the full ARGOS-SHAW- prefixed station id, so the PKI material is
    # reused across naming changes. The CN/SAN in the issued cert still
    # uses the full id for unambiguous identification on the wire.
    from stations import short_id  # local import keeps the CLI import-light

    for st in STATIONS:
        sid = short_id(st.station_id)
        leaf_key, leaf_cert = mint_leaf(
            station_id=st.station_id, ca_key=ca_key, ca_cert=ca_cert
        )
        cert_secret = _ensure_secret(
            sm, args.project, f"drone-sensor-dev-sim-cert-{sid}"
        )
        key_secret = _ensure_secret(
            sm, args.project, f"drone-sensor-dev-sim-key-{sid}"
        )
        _add_version(sm, cert_secret, _cert_pem(leaf_cert))
        _add_version(sm, key_secret, _key_pem(leaf_key))

        pub_path = out_dir / f"{sid}.pub.pem"
        pub_path.write_bytes(_pubkey_pem(leaf_key))

        log.info(
            "%s minted; secrets cert=%s key=%s pubkey=%s",
            st.station_id,
            cert_secret.split("/")[-1],
            key_secret.split("/")[-1],
            pub_path,
        )

    log.info("done; %d stations minted", len(STATIONS))


if __name__ == "__main__":
    main()
