# Gateway tests

Focused unit tests around the security-critical paths.

```
cd backend/gateway
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio
bash scripts/generate_protos.sh    # populates ../proto_gen
export PYTHONPATH=src:proto_gen
pytest tests/
```

Tests:

- `test_state_machine.py` — transition matrix, extra-confirmation set,
  connectable / publish-audio guards, normalize() on legacy values.
- `test_service_branching.py` — StreamAudio dispatch on `active`,
  `lost`, and `wipe_requested` device states. Uses an in-memory async
  iterator and a fake `ServicerContext` to avoid standing up a real
  gRPC server.
