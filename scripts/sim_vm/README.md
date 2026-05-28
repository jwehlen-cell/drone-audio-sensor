# VM-hosted simulator

Provisions a tiny GCE VM in `us-central1-a` that runs
`scripts/simulate_soh_phones.py` continuously, sending an audio-burst cycle
every 3 minutes against the project's gateway. Intentionally **not** managed
by terraform — this is a hand-managed test/utility VM. Free-tier eligible
(`e2-micro` in `us-central1`).

## Provision

```bash
./scripts/sim_vm/provision.sh
```

Creates an `e2-micro` VM named `drone-sim-sender` in `us-central1-a`, runs a
startup script that:

1. Installs Python 3 + venv + git
2. Clones this repo to `/opt/drone-audio-sensor`
3. Sets up `.venv` and installs `scripts/requirements.txt`
4. Installs and enables a systemd unit (`drone-simulator.service`) that
   runs the simulator as the `drone-sim` user, with `Restart=always`

End-to-end ~3-5 min after `provision.sh` returns.

## Status

```bash
./scripts/sim_vm/status.sh
```

Shows VM state, systemd service status, and the tail of
`/var/log/drone-simulator.log`.

## Update

```bash
./scripts/sim_vm/update.sh
```

SSHes in, `git pull` on `main`, restarts the service. Use this any time you
land a commit you want the VM to pick up.

## Destroy

```bash
./scripts/sim_vm/destroy.sh
```

Deletes the VM. (Free tier is per-month, so leaving it running long-term
costs nothing as long as it's the only `e2-micro` in `us-central1`.)

## Environment

The provision script reads these env vars (all have sensible defaults):

| Var          | Default                                                 |
|--------------|---------------------------------------------------------|
| `PROJECT`    | `drone-audio-sensor`                                    |
| `ZONE`       | `us-central1-a`                                         |
| `INSTANCE`   | `drone-sim-sender`                                      |
| `GATEWAY_URL`| `drone-sensor-dev-gateway-65av54lbuq-uc.a.run.app`      |
| `REPO_URL`   | `https://github.com/jwehlen-cell/drone-audio-sensor.git`|

## Why not terraform?

The VM is a test/demo asset that comes and goes; it shouldn't live in the
infra graph alongside the production services. If you ever want to promote
it to permanent infra, lift the `gcloud compute instances create` flags
from `provision.sh` into a `google_compute_instance` block in `iac/terraform/`.
