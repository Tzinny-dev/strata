# Strict contract mode

`strata build` keeps its permissive behavior by default. With `--strict`,
it requires that every checked model declare `-> contract N`:

```sh
strata build /home/carlos/Documentos/projects/code/prototype/examples/daily_orders.strata --strict
strata build /home/carlos/Documentos/projects/code/prototype/examples/daily_orders.strata daily_orders --strict
```

Put the model names before `--strict`: the current parser does not allow
interleaving this flag between the file and the model names.

## Scope and errors

- With no selection, it checks all models in the loaded project.
- With a selection, it requires a contract on the selected models **and all
  their transitive dependencies** checked by the typechecker. Models outside
  that graph do not block the build.
- It does not require contracts on sources: these declare their own schema.
- After the typecheck, if contracts are missing, it writes `E014` to stderr,
  lists affected models alphabetically and returns **2**, without a success report.
- Declared contracts keep being verified as always: missing columns (E010),
  incompatible types (E011), nullability (E012), constraints on strings
  (E013) and unknown contracts (E061). A prior analysis error keeps its
  diagnostic and exit code **1**.
- A successful build returns **0** and keeps the usual report.

This is a static, opt-in guarantee of the `build` command, not a physical
validation of the warehouse nor a global mode for `run`, `compile` or `check`.
It does not require exact column equality: it preserves the existing contract
compatibility. `lint --strict` remains independent: it turns its warnings into
exit code 2, whereas `build --strict` requires contracts, not the absence of lint.

In CI, use `build --strict` as a blocking step before compiling or running.
