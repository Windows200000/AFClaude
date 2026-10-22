"""Login factors and their rules for the dashboard (phase 6a W6, docs/phase6a_plan.md §3.6;
design §9.3 / §9.5; D-150, D-159, D-179).

  passwords.py  Argon2id password hashes (PHC strings, rehash on a parameter change, the floor)
                and the password rule (12–128 characters)
  totp.py       TOTP (RFC 6238): seeds, codes, the ±1-step window and the replay rule
  recovery.py   the 10 one-time recovery codes: generation, peppered hashes, matching
  factors.py    the factor rule: which factors make a complete login (remembered device, step-up)
  ratelimit.py  the login rate limits and their backoff, as arithmetic over failure times

Built so far: W6's pure part (no tables, no endpoints, no sessions). Python 3.12 with
cryptography (in the .venv through mcp), mypy --strict.
"""
