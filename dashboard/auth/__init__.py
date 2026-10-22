"""Login factors, passkeys and their rules for the dashboard (phase 6a W6, docs/phase6a_plan.md
§3.6; phase 6b, docs/phase6b_plan.md; design §9.3 / §9.5; D-150, D-159, D-179).

  passwords.py  Argon2id password hashes (PHC strings, rehash on a parameter change, the floor)
                and the password rule (12–128 characters)
  totp.py       TOTP (RFC 6238): seeds, codes, the ±1-step window and the replay rule
  recovery.py   the 10 one-time recovery codes: generation, peppered hashes, matching
  factors.py    the factor rule: which factors make a complete login (remembered device, step-up),
                incl. 6b's hardware / synced passkey and OIDC
  ratelimit.py  the login rate limits and their backoff, as arithmetic over failure times
  mds.py        the FIDO Metadata Service list (6b P1): fetch, verify (pinned GlobalSign Root R46,
                fido_mds_root_r46.der), the cache record, which models count as hardware
  passkeys.py   the passkey policy over fido2's types (6b P2): classify a registration (hardware /
                synced / refused) and check an assertion (UV, BE/BS, sign counter, revoked models)

Built so far: W6's and 6b P1/P2's pure parts (no tables, no endpoints, no sessions). Python 3.12
with cryptography (in the .venv through mcp) and fido2 (requirements-dashboard.txt), mypy --strict.
"""
