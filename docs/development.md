# Flujo de desarrollo

## Ramas

```
main   ← solo vía PR desde dev. Siempre verde. Es lo que se usa en el día a día.
dev    ← integración. Recibe PRs de ramas de trabajo.
feat/… fix/… chore/…  ← una rama por cambio, creada desde dev.
```

1. `git switch dev && git pull && git switch -c feat/mcp-server`
2. Commits pequeños con [Conventional Commits](https://www.conventionalcommits.org/es):
   `feat(tasks): …`, `fix(sync): …`, `docs: …`, `test: …`, `chore: …`.
3. `./scripts/check.sh` en local antes de abrir PR.
4. PR `feat/… → dev`. La CI debe pasar.
5. Cuando `dev` tenga un conjunto coherente: PR `dev → main` ("release").

## CI

`.github/workflows/ci.yml` ejecuta `scripts/check.sh` (formato, lint, fronteras
arquitectónicas, tests unitarios + e2e con el bridge JS en Node) en cada PR y push a
`main`/`dev`. Los tests `live` (iCloud real) no corren en CI: requieren credenciales
personales y se lanzan a mano con `POS_LIVE=1`.

## "CD"

No hay nada desplegado: el Personal OS se ejecuta en local. Tras mergear a `main`:

```bash
git switch main && git pull && uv sync
uv run pos init              # aplica migraciones nuevas
uv run pos bridge install    # si cambió el bridge del iPhone
```

## Claude Code

- No hace commits ni PRs sin autorización explícita.
- Trabaja en ramas `feat/…` desde `dev`, nunca directamente en `main`.
