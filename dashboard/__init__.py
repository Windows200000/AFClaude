"""The dashboard's backend (design §4, §8; docs/phase6a_plan.md): a Starlette app that serves the
JSON API under /api/v1 and the built TypeScript frontend, and keeps everything that decides or
protects (login, sessions, roles, CSRF, the single write path through actions.py, D-209).

  app.py        create_app(): routes from the endpoint table, the app shell, the error handlers
  routes.py     the endpoint table (the only list of API routes) and its dispatcher
  security.py   headers and CSP, the Host check, the write guard (§9.8)
  errors.py     the one error shape (§8) and ApiError
  api_types.py  every request and response model (the API schema, D-209)
  codegen.py    schema.json + the frontend's TS types from api_types.py (python -m dashboard.codegen)

Python 3.12 (the .venv and the image), mypy --strict. Nothing here listens on a port by itself.
"""
