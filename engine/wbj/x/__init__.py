"""X (Twitter) content engine — analiza engagement, redacta y encola posts.

Mismo contrato que el resto del motor: sin evidencia, no hay número. Un
patrón ("los hilos funcionan mejor") solo se declara si trae su n, su
tamaño de efecto y su p-value. Todo lo demás se reporta como INSUFICIENTE.

Módulos:
- `analytics`: lee el CSV de analytics.x.com y mide QUÉ funciona en TU cuenta.
- `brief`:     arma paquetes de datos duros (desde Reportes/ y el motor).
- `generate`:  Claude redacta en inglés sobre el brief; los números se validan.
- `store`:     cola de borradores en disco (JSONL), estado y auditoría.
"""
