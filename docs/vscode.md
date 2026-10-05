# VS Code extension

The **Strata — Tzinny** extension (`Tzinny-dev.strata-tzinny`) supports
`.strata` files in VS Code: syntax highlighting and an LSP client
built on `strata lsp`.

## Install

From the [Visual Studio Marketplace](https://marketplace.visualstudio.com/items?itemName=Tzinny-dev.strata-tzinny)
or via CLI:

```bash
code --install-extension Tzinny-dev.strata-tzinny
```

## Requirements

- `strata` on the `PATH` (`pip install strata-lang`, `uv tool install
  strata-lang` or the standalone binary), **or** the setting
  `strata.binaryPath` pointing to the binary
  (`"/path/to/.venv/bin/strata"` for venv environments).
- The LSP server is the CLI itself: `strata lsp` (stdio JSON-RPC, no
  extra dependencies).

If the binary is not found, the extension reports it — the
**Strata: Show Version** action (`strata.showVersion`) checks the
configured binary and shows the error with the `strata.binaryPath`
hint.

## What it provides

- **Syntax highlighting** (TextMate grammar `source.strata`) and
  language configuration (`//` comments, auto-closing pairs).
- **Diagnostics** live — the same `Checker` that runs
  `strata check`: types, contracts and lineage (E070, E030…).
- **Completion** — models, sources, catalog functions and columns.
- **Hover** — type of the column under the cursor.
- **Definition** — jump to the source model of a column.

## Settings

| Setting | Default | Description |
|---|---|---|
| `strata.binaryPath` | `"strata"` | Path to the `strata` binary that is launched as the LSP server |

## Development (local rebuild)

```bash
cd vscode
npm install
npm run compile          # tsc → out/extension.js
npx vsce package         # → strata-tzinny-<version>.vsix
code --install-extension strata-tzinny-<version>.vsix
```

The extension version follows that of `strata-lang` (currently `0.1.5`).
See also `guide/binary-standalone.md` (`strata lsp` entry in the
standalone binary) and the `README` of `vscode/` in the repository.
