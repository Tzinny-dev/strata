# VS Code extension

La extensión **Strata — Tzinny** (`Tzinny-dev.strata-tzinny`) da soporte
a archivos `.strata` en VS Code: resaltado de sintaxis y un cliente LSP
construido sobre `strata lsp`.

## Instalar

Desde la [Visual Studio Marketplace](https://marketplace.visualstudio.com/items?itemName=Tzinny-dev.strata-tzinny)
o por CLI:

```bash
code --install-extension Tzinny-dev.strata-tzinny
```

## Requisitos

- `strata` en el `PATH` (`pip install strata-lang`, `uv tool install
  strata-lang` o el binario standalone), **o** bien la setting
  `strata.binaryPath` apuntando al binario
  (`"/ruta/a/.venv/bin/strata"` para entornos venv).
- El servidor LSP es el propio CLI: `strata lsp` (stdio JSON-RPC, sin
  dependencias extra).

Si el binario no se encuentra, la extensión lo reporta — la acción
**Strata: Show Version** (`strata.showVersion`) comprueba el binario
configurado y muestra el error con el hint de `strata.binaryPath`.

## Qué aporta

- **Resaltado de sintaxis** (grammar TextMate `source.strata`) y
  configuración de lenguaje (`//` comentarios, cierre de pares).
- **Diagnostics** en vivo — el mismo `Checker` que ejecuta
  `strata check`: tipos, contratos y lineage (E070, E030…).
- **Completion** — modelos, sources, funciones del catálogo y columnas.
- **Hover** — tipo de la columna bajo el cursor.
- **Definition** — salto al modelo de origen de una columna.

## Ajustes

| Setting | Default | Descripción |
|---|---|---|
| `strata.binaryPath` | `"strata"` | Ruta al binario `strata` que se lanza como servidor LSP |

## Desarrollo (rebuild local)

```bash
cd vscode
npm install
npm run compile          # tsc → out/extension.js
npx vsce package         # → strata-tzinny-<versión>.vsix
code --install-extension strata-tzinny-<versión>.vsix
```

La versión de la extensión sigue la de `strata-lang` (hoy `0.1.5`).
Ver también `guide/binary-standalone.md` (entrada `strata lsp` en el
binario standalone) y el `README` de `vscode/` en el repositorio.
