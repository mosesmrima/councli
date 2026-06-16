# councli security migrations

This guide defines the security migration path for project configs, trust pins,
artifact schemas, and local cleanup behavior. Use it before upgrading a project
or publishing a release that changes command, trust, adapter, or artifact
semantics.

## Migration principles

- Project `.councli/config.yaml` is not trusted by itself.
- User-local trust pins authorize command-bearing fields.
- A migration may add schema metadata without changing trusted command fields.
- Any change to commands, binaries, capabilities, tmux controls, sandbox
  wrappers, or prompt transport requires user review and `councli trust`.
- Raw artifacts, logs, blackboards, and support bundles are sensitive local
  data and should be redacted before sharing.

## Config schema v1

`councli.config.v1` is the first explicit project config schema. Legacy configs
without `schema_version` can be inspected and upgraded in place.

Recommended migration:

```bash
councli config check
councli trust --dry-run
councli config migrate
councli trust --dry-run
councli trust
councli doctor --security
```

Expected behavior:

- `config check` reports whether the config is legacy or current.
- `trust --dry-run` previews command-field and binary drift without writing a
  new trust pin.
- `config migrate` adds the schema marker while preserving command-bearing
  values.
- `trust` should only be run after reviewing the dry-run output.
- `doctor --security` confirms intent readiness, binary path/hash/version
  status, and elevated command surfaces.

## Trust pin migration

Trust pins record:

- project identity;
- trusted agent control fields;
- resolved binary paths;
- executable SHA-256 hashes;
- version metadata when available.

Run `councli trust --dry-run` after:

- editing `.councli/config.yaml`;
- installing or upgrading an assistant CLI;
- changing `PATH`;
- moving or copying a project directory;
- adding `sandbox_wrapper`;
- changing tmux session names, detach keys, or native launch commands.

If a project was intentionally moved, run:

```bash
councli trust --repair-identity
```

Only do this after confirming the new path is the intended project.

## Artifact and support-bundle migration

Before sharing artifacts:

```bash
councli verify latest
councli recover latest
councli artifacts scrub --dry-run
councli artifacts export --redacted
```

Use `artifacts scrub --write` only when you intentionally want to rewrite local
artifact files in place. Redacted export is safer for support bundles because it
leaves the original evidence untouched.

## Worktree cleanup migration

Implementation worktrees are kept by default. To inspect abandoned councli
worktrees:

```bash
councli worktrees prune --status abandoned --dry-run
```

To remove only safe registered councli-created worktrees:

```bash
councli worktrees prune --status abandoned --delete
```

`worktrees prune` refuses to remove paths outside the expected
`.councli-worktrees/<repo>` location, unregistered paths, non-`councli/`
branches, and active or completed-but-unapplied worktrees.

## Rollback

If a migration or trust update behaves incorrectly:

1. Restore the previous git commit or package version.
2. Restore `.councli/config.yaml` from version control or backup.
3. Remove the new user-local trust pin only if you need to force a fresh trust
   review.
4. Run `councli trust --dry-run`.
5. Run `councli doctor --security`.

Do not delete `.councli/runs/` while debugging a migration. Run artifacts are
the evidence needed for `verify`, `recover`, and support-bundle export.
