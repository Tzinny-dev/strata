# Strata for VS Code

`strata` LSP client — diagnostics, completion, hover, definition over `.strata` files.

## Requirements

* `strata` on `PATH` (`pip install strata-lang` or standalone `strata` `0.1.3`), or set `strata.binaryPath` in settings.
* The LSP is `strata lsp` (`strata/lsp.py:243`) — stdio JSON-RPC, no deps.

## Features

* `publishDiagnostics` from `Checker` (`strata/lsp.py:100` `reload`)
* `completion` — models, sources, `FUNCTIONS` catalog, columns (`lsp.py:157`)
* `hover` — column type (`lsp.py:189`)
* `definition` — model jump (`lsp.py:222`)

## Settings

* `strata.binaryPath` — path to `strata` binary (default `"strata"`). Use `"/path/to/.venv/bin/strata"` for venv.

## Dev

```bash
cd vscode
npm install
npm run compile
# package
npx vsce package
# install
code --install-extension strata-lang-0.1.3.vsix
```

See `../docs-site/guide/binary-standalone.md:71` for LSP entry `strata lsp`.
