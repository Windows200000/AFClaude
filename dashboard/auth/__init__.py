"""Passkeys and OIDC for the dashboard (phase 6b, docs/phase6b_plan.md; D-150, D-159).

  mds.py       the FIDO Metadata Service list: fetch, verify (pinned GlobalSign Root R46,
               fido_mds_root_r46.der), the cache record, which models count as hardware
  passkeys.py  the passkey policy over fido2's types: classify a registration (hardware /
               synced / refused) and check an assertion (UV, BE/BS, sign counter, revoked models)

Built so far: steps P1 and P2's pure part (no tables, no endpoints yet). Python 3.12 with fido2
(requirements-dashboard.txt), mypy --strict.
"""
