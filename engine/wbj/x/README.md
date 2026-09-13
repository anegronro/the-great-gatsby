# Content engine para X (Creator Studio)

Analiza el engagement real de la cuenta, redacta posts en inglés sobre datos
duros del motor, y los deja en una cola de aprobación. **No publica nada.**

## Lo que hay que saber antes de usarlo

**La API de X ya no tiene free tier** (murió el 6 de febrero de 2026). Es
pay-per-use con créditos prepagados en `console.x.com`:

| Acción | Costo |
|---|---|
| Crear un post | $0.015 |
| Crear un post **con link** | **$0.20** |
| Leer un post | $0.005 |
| Leer un usuario | $0.010 |

10 posts diarios sin links cuestan ~$4.50/mes; con link, $60. Por eso el
redactor tiene prohibido incluir URLs — y además los links hunden el alcance
orgánico, que este módulo mide.

**El requisito de monetización es de audiencia, no técnico**: X Premium
activo + 500 seguidores verificados + 5M impresiones orgánicas en 3 meses,
contando solo impresiones de usuarios Premium. La automatización da
consistencia y volumen; no compra impresiones.

**El revenue share paga por los anuncios en la sección de replies.** Un post
que informa y se acaba no monetiza; uno que abre discusión, sí. Por eso la
métrica primaria de todo el módulo es `reply_rate`, no likes.

## Uso

```bash
# 1. Qué funciona en TU cuenta (descarga el CSV de analytics.x.com -> Posts)
wbj x analizar ~/Downloads/posts.csv

# 2. Redactar desde los datos del motor, aplicando el estilo medido
wbj x redactar --ticker NVDA --csv ~/Downloads/posts.csv

# 3. Revisar
wbj x cola
wbj x aprobar <id> --texto "versión editada"
wbj x rechazar <id> --motivo "cifra sin fuente"
```

## Por qué no miente

- **Números**: el brief precomputa todas las cifras permitidas (incluidos
  derivados como el upside). `validate_numbers` es determinista, corre sin
  modelo, y marca cualquier cifra que no salga del brief. Un número
  inventado no llega a la cola.
- **Patrones**: ningún hallazgo se declara sin n mínimo (8 por grupo),
  tamaño de efecto sobre la **mediana** (no la media: las distribuciones de
  engagement son long-tail) y p-value por Mann-Whitney U.
- **Comparaciones múltiples**: se prueban ~8 features a la vez, así que un
  p<0.05 aislado es un falso positivo esperado, no un hallazgo. Se aplica
  Benjamini-Hochberg; solo lo que sobrevive se le pasa al redactor como
  regla de estilo. Esto no es teórico — durante el desarrollo, una feature
  de ruido puro dio p=0.041 y habría entrado como "lección".
- **Nada se borra**: los rechazos quedan en la cola. La tasa de aprobación
  es el criterio para pasar de revisión manual a auto-publicación
  (>=80% sobre >=20 decididos).

## Lo que falta

- Publicador contra la API de X (créditos + OAuth).
- Cola de aprobación por Telegram con botones (el `.env` ya trae
  `TELEGRAM_BOT_TOKEN`).
- Cosecha de engagement real por post publicado, para cerrar el bucle
  predicción -> resultado como hace `wbj track` con los precios.
