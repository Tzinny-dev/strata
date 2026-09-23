# Modo estricto de contratos

`strata build` conserva su comportamiento permisivo por defecto. Con `--strict`,
exige que cada modelo comprobado declare `-> contract N`:

```sh
strata build /home/carlos/Documentos/projects/code/prototype/examples/daily_orders.strata --strict
strata build /home/carlos/Documentos/projects/code/prototype/examples/daily_orders.strata daily_orders --strict
```

Coloca los nombres de modelo antes de `--strict`: el parser actual no admite
intercalar este flag entre el archivo y los nombres de modelo.

## Alcance y errores

- Sin selección, comprueba todos los modelos del proyecto cargado.
- Con selección, exige contrato a los modelos seleccionados **y a todas sus
  dependencias transitivas** comprobadas por el typechecker. Los modelos ajenos
  a ese grafo no bloquean el build.
- No exige contratos a las fuentes: estas declaran su propio esquema.
- Tras el typecheck, si faltan contratos, escribe `E014` en stderr, lista los
  modelos afectados en orden alfabético y retorna **2**, sin informe de éxito.
- Los contratos declarados siguen verificándose como siempre: columnas ausentes
  (E010), tipos incompatibles (E011), nulabilidad (E012), restricciones sobre
  strings (E013) y contratos desconocidos (E061). Un error previo de análisis
  conserva su diagnóstico y código de salida **1**.
- Un build correcto retorna **0** y conserva el informe habitual.

Esto es una garantía estática y optativa del comando `build`, no una validación
física del warehouse ni un modo global para `run`, `compile` o `check`.
No exige igualdad exacta de columnas: conserva la compatibilidad de contratos
existente. `lint --strict` permanece independiente: convierte sus advertencias
en salida 2, mientras que `build --strict` exige contratos, no ausencia de lint.

En CI, usa `build --strict` como paso bloqueante antes de compilar o ejecutar.
