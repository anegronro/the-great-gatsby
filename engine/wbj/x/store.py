"""Cola de borradores en disco. Append-only en espíritu: nada se borra.

Un borrador rechazado se marca, no se elimina — igual que las predicciones
del motor. El historial de qué se rechazó y por qué es la señal que después
permite medir si el generador mejoró.

Archivo: <repo>/Contenido/cola.jsonl (una línea por borrador).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ESTADOS = ("pendiente", "aprobado", "rechazado", "publicado")


@dataclass
class Item:
    """Un borrador y todo su rastro de auditoría."""

    id: str
    creado: str
    estado: str
    texto: str
    hilo: list[str] = field(default_factory=list)
    ticker: str = ""
    kind: str = ""
    approach: str = ""
    reply_hook: str = ""
    brief_id: str = ""
    model: str = ""
    numeros_no_justificados: list[str] = field(default_factory=list)
    truncado: bool = False
    # Se llenan al decidir / publicar / cosechar.
    texto_editado: str = ""
    decidido: str = ""
    motivo_rechazo: str = ""
    publicado_en: str = ""
    post_id: str = ""
    metricas: dict = field(default_factory=dict)

    @property
    def texto_final(self) -> str:
        """Lo que se publicaría: la edición de Angel gana sobre el original."""
        return self.texto_editado or self.texto


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Queue:
    """Cola persistente de borradores."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[Item]:
        if not self.path.exists():
            return []
        items: list[Item] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(Item(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue  # una línea corrupta no debe tumbar la cola entera
        return items

    def _write(self, items: list[Item]) -> None:
        """Reescritura atómica: tmp + rename, para no perder la cola si algo falla."""
        tmp = self.path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "\n".join(json.dumps(asdict(i), ensure_ascii=False) for i in items) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def add(self, draft, *, estado: str = "pendiente") -> Item:
        """Encola un `generate.Draft`."""
        item = Item(
            id=uuid.uuid4().hex[:10],
            creado=_now(),
            estado=estado,
            texto=draft.text,
            hilo=list(draft.thread),
            ticker=draft.ticker,
            kind=draft.kind,
            approach=draft.approach,
            reply_hook=draft.reply_hook,
            brief_id=draft.brief_id,
            model=draft.model,
            numeros_no_justificados=list(draft.numeros_no_justificados),
            truncado=getattr(draft, "truncado", False),
        )
        items = self.load()
        items.append(item)
        self._write(items)
        return item

    def get(self, item_id: str) -> Item | None:
        return next((i for i in self.load() if i.id == item_id), None)

    def pendientes(self) -> list[Item]:
        return [i for i in self.load() if i.estado == "pendiente"]

    def update(self, item_id: str, **campos) -> Item | None:
        """Cambia campos de un item. Devuelve el item actualizado, o None."""
        items = self.load()
        found = None
        for i in items:
            if i.id == item_id:
                for k, v in campos.items():
                    if hasattr(i, k):
                        setattr(i, k, v)
                found = i
                break
        if found is None:
            return None
        self._write(items)
        return found

    def aprobar(self, item_id: str, texto_editado: str = "") -> Item | None:
        return self.update(
            item_id, estado="aprobado", decidido=_now(),
            texto_editado=texto_editado,
        )

    def rechazar(self, item_id: str, motivo: str = "") -> Item | None:
        return self.update(
            item_id, estado="rechazado", decidido=_now(), motivo_rechazo=motivo,
        )

    def marcar_publicado(self, item_id: str, post_id: str = "") -> Item | None:
        return self.update(
            item_id, estado="publicado", publicado_en=_now(), post_id=post_id,
        )

    def resumen(self) -> dict[str, int]:
        items = self.load()
        return {e: sum(1 for i in items if i.estado == e) for e in ESTADOS}

    def tasa_aprobacion(self) -> tuple[float | None, int]:
        """Aprobados / decididos, y cuántos se decidieron.

        Es el criterio para pasar de cola manual a publicación automática:
        cuando Angel aprueba 4 de cada 5 sin editar, el generador se ganó
        la autonomía. Antes de eso, no.
        """
        decididos = [i for i in self.load()
                     if i.estado in ("aprobado", "rechazado", "publicado")]
        if not decididos:
            return None, 0
        ok = sum(1 for i in decididos if i.estado != "rechazado")
        return ok / len(decididos), len(decididos)
