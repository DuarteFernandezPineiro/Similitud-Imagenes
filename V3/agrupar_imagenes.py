#!/usr/bin/env python3
"""Agrupa imágenes parecidas de un árbol de carpetas.

La similitud se calcula con un hash perceptual (pHash) de 64 bits. No se
modifican ni se copian las imágenes: el resultado se guarda como JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from array import array
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations, islice
from pathlib import Path
from typing import Callable, Iterable, Iterator

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener


IMAGE_EXTENSIONS = {
    ".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".jxl", ".png",
    ".tif", ".tiff", ".webp",
}
ALLOWED_THRESHOLDS = (80, 90, 95, 100)
HASH_BITS = 64

register_heif_opener()


def dct_matrix(size: int = 32) -> np.ndarray:
    """Matriz DCT-II ortonormal, calculada una vez por proceso."""
    rows = np.arange(size, dtype=np.float32)[:, None]
    cols = np.arange(size, dtype=np.float32)[None, :]
    matrix = np.cos(np.pi * (2 * cols + 1) * rows / (2 * size))
    matrix[0, :] *= 1 / np.sqrt(size)
    matrix[1:, :] *= np.sqrt(2 / size)
    return matrix.astype(np.float32)


_DCT = dct_matrix()


def perceptual_hash(path_as_text: str) -> tuple[str, int | None, str | None]:
    """Devuelve ruta, pHash y, si falla, el motivo sin detener el análisis."""
    try:
        with Image.open(path_as_text) as image:
            image = ImageOps.exif_transpose(image).convert("L")
            image = image.resize((32, 32), Image.Resampling.LANCZOS)
            pixels = np.asarray(image, dtype=np.float32)

        coefficients = _DCT @ pixels @ _DCT.T
        low_frequency = coefficients[:8, :8]
        # Se omite el componente DC para que el brillo global afecte menos.
        median = np.median(low_frequency.flat[1:])
        bits = (low_frequency >= median).astype(np.uint8).ravel()
        image_hash = int.from_bytes(np.packbits(bits, bitorder="big").tobytes(), "big")
        return path_as_text, image_hash, None
    except (OSError, UnidentifiedImageError, ValueError) as error:
        return path_as_text, None, str(error)


@dataclass(frozen=True)
class ImageFile:
    path: str
    size: int
    modified_ns: int


@dataclass(frozen=True)
class CachedHash:
    size: int
    modified_ns: int
    value: int


@dataclass(frozen=True)
class CachedFailure:
    size: int
    modified_ns: int
    error: str


def stored_hash_parts(image_hash: int) -> tuple[int, int, int, int, int, int, int]:
    """Fragmentos indexados por SQLite para los planes de búsqueda 80/90/95%."""
    return (
        (image_hash >> 48) & 0xFFFF,
        (image_hash >> 32) & 0xFFFF,
        (image_hash >> 16) & 0xFFFF,
        image_hash & 0xFFFF,
        (image_hash >> 43) & ((1 << 21) - 1),
        (image_hash >> 22) & ((1 << 21) - 1),
        image_hash & ((1 << 22) - 1),
    )


class HashCache:
    """Caché e índice persistentes de hashes y grupos."""

    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path)
        # WAL evita bloqueos largos al actualizar la caché y NORMAL reduce
        # sincronizaciones de disco sin comprometer la integridad transaccional.
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA temp_store=MEMORY")
        self.connection.execute("PRAGMA cache_size=-32768")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS image_hashes (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                value BLOB NOT NULL,
                group_id INTEGER,
                p16_0 INTEGER,
                p16_1 INTEGER,
                p16_2 INTEGER,
                p16_3 INTEGER,
                p21_0 INTEGER,
                p21_1 INTEGER,
                p21_2 INTEGER
            )
            """
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS group_state (id INTEGER PRIMARY KEY CHECK (id = 1), threshold INTEGER NOT NULL)"
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS unreadable_files (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                error TEXT NOT NULL
            )
            """
        )
        existing_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(image_hashes)")}
        for name in ("group_id", "p16_0", "p16_1", "p16_2", "p16_3", "p21_0", "p21_1", "p21_2"):
            if name not in existing_columns:
                self.connection.execute(f"ALTER TABLE image_hashes ADD COLUMN {name} INTEGER")
        for name in ("group_id", "p16_0", "p16_1", "p16_2", "p16_3", "p21_0", "p21_1", "p21_2"):
            self.connection.execute(f"CREATE INDEX IF NOT EXISTS idx_image_hashes_{name} ON image_hashes({name})")
        self._backfill_parts()
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def read_all(self) -> dict[str, CachedHash]:
        return {
            path: CachedHash(size, modified_ns, int.from_bytes(value, "big"))
            for path, size, modified_ns, value in self.connection.execute(
                "SELECT path, size, modified_ns, value FROM image_hashes"
            )
        }

    def read_unreadable(self) -> dict[str, CachedFailure]:
        return {
            path: CachedFailure(size, modified_ns, error)
            for path, size, modified_ns, error in self.connection.execute(
                "SELECT path, size, modified_ns, error FROM unreadable_files"
            )
        }

    def save_unreadable(self, failures: dict[str, tuple[ImageFile, str]]) -> None:
        if not failures:
            return
        self.connection.executemany(
            """
            INSERT INTO unreadable_files(path, size, modified_ns, error)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size=excluded.size,
                modified_ns=excluded.modified_ns,
                error=excluded.error
            """,
            (
                (path, file.size, file.modified_ns, error)
                for path, (file, error) in failures.items()
            ),
        )
        self.connection.commit()

    def remove_unreadable(self, paths: Iterable[str]) -> None:
        items = list(paths)
        if not items:
            return
        self.connection.executemany("DELETE FROM unreadable_files WHERE path = ?", ((path,) for path in items))
        self.connection.commit()

    def _backfill_parts(self) -> None:
        rows = self.connection.execute("SELECT rowid, value FROM image_hashes WHERE p16_0 IS NULL").fetchall()
        if not rows:
            return
        self.connection.executemany(
            """
            UPDATE image_hashes
            SET p16_0=?, p16_1=?, p16_2=?, p16_3=?, p21_0=?, p21_1=?, p21_2=?
            WHERE rowid=?
            """,
            [(*stored_hash_parts(int.from_bytes(value, "big")), row_id) for row_id, value in rows],
        )

    def save(self, hashes: dict[str, tuple[ImageFile, int]]) -> None:
        self.connection.executemany(
            """
            INSERT INTO image_hashes(
                path, size, modified_ns, value, group_id,
                p16_0, p16_1, p16_2, p16_3, p21_0, p21_1, p21_2
            )
            VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size = excluded.size,
                modified_ns = excluded.modified_ns,
                value = excluded.value,
                group_id = NULL,
                p16_0 = excluded.p16_0,
                p16_1 = excluded.p16_1,
                p16_2 = excluded.p16_2,
                p16_3 = excluded.p16_3,
                p21_0 = excluded.p21_0,
                p21_1 = excluded.p21_1,
                p21_2 = excluded.p21_2
            """,
            (
                (path, file.size, file.modified_ns, image_hash.to_bytes(8, "big"), *stored_hash_parts(image_hash))
                for path, (file, image_hash) in hashes.items()
            ),
        )
        self.connection.commit()

    def remove(self, paths: Iterable[str]) -> None:
        items = list(paths)
        if not items:
            return
        self.connection.executemany("DELETE FROM image_hashes WHERE path = ?", ((path,) for path in items))
        self.connection.commit()

    def has_uninitialized_groups(self) -> bool:
        return self.connection.execute("SELECT EXISTS(SELECT 1 FROM image_hashes WHERE group_id IS NULL)").fetchone()[0] == 1

    def saved_threshold(self) -> int | None:
        row = self.connection.execute("SELECT threshold FROM group_state WHERE id = 1").fetchone()
        return row[0] if row else None

    def set_threshold(self, threshold: int) -> None:
        self.connection.execute(
            "INSERT INTO group_state(id, threshold) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET threshold=excluded.threshold",
            (threshold,),
        )
        self.connection.commit()

    def all_records(self) -> list[tuple[int, str, int]]:
        return [
            (row_id, path, int.from_bytes(value, "big"))
            for row_id, path, value in self.connection.execute("SELECT rowid, path, value FROM image_hashes")
        ]

    def initialize_groups(self, paths: Iterable[str]) -> None:
        items = list(paths)
        if not items:
            return
        self.connection.executemany(
            "UPDATE image_hashes SET group_id = rowid WHERE path = ? AND group_id IS NULL",
            ((path,) for path in items),
        )
        self.connection.commit()

    def replace_groups(self, row_ids: list[int], groups: list[list[int]]) -> None:
        """Recrea todos los componentes conectados, usado solo cuando es necesario."""
        self.connection.execute("UPDATE image_hashes SET group_id = rowid")
        assignments: list[tuple[int, int]] = []
        for members in groups:
            group_id = min(row_ids[position] for position in members)
            assignments.extend((group_id, row_ids[position]) for position in members)
        self.connection.executemany("UPDATE image_hashes SET group_id = ? WHERE rowid = ?", assignments)
        self.connection.commit()

    def records_for_paths(self, paths: Iterable[str]) -> list[tuple[int, str, int, int]]:
        result: list[tuple[int, str, int, int]] = []
        items = list(paths)
        for start in range(0, len(items), 500):
            chunk = items[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"SELECT rowid, path, value, group_id FROM image_hashes WHERE path IN ({placeholders})",
                chunk,
            )
            result.extend((row[0], row[1], int.from_bytes(row[2], "big"), row[3]) for row in rows)
        return result

    def similar_group_ids(self, row_id: int, image_hash: int, threshold: int) -> set[int]:
        """Consulta SQLite solo para las candidatas de una imagen recién añadida."""
        max_distance = max_hamming_distance(threshold)
        plan = plan_for(max_distance)
        values = split_hash(image_hash, plan.widths)
        columns = (
            ("p16_0", "p16_1", "p16_2", "p16_3")
            if plan.widths == (16, 16, 16, 16)
            else ("p21_0", "p21_1", "p21_2")
        )
        matching_groups: set[int] = set()
        inspected: set[int] = set()

        if max_distance == 0:
            queries = [("value", [image_hash.to_bytes(8, "big")])]
        else:
            queries = [
                (column, [value ^ mask for mask in masks(width, plan.local_radius)])
                for column, value, width in zip(columns, values, plan.widths)
            ]
        for column, alternatives in queries:
            for start in range(0, len(alternatives), 500):
                selected = alternatives[start:start + 500]
                placeholders = ",".join("?" for _ in selected)
                statement = (
                    f"SELECT rowid, value, group_id FROM image_hashes "
                    f"WHERE {column} IN ({placeholders}) AND rowid != ?"
                )
                for candidate_id, candidate_value, group_id in self.connection.execute(statement, (*selected, row_id)):
                    if candidate_id in inspected:
                        continue
                    inspected.add(candidate_id)
                    if (image_hash ^ int.from_bytes(candidate_value, "big")).bit_count() <= max_distance:
                        matching_groups.add(group_id)
        return matching_groups

    def merge_groups(self, destination: int, group_ids: Iterable[int], *, commit: bool = True) -> None:
        """Fusiona únicamente los grupos que una foto nueva haya conectado."""
        source_groups = list(set(group_ids) - {destination})
        for start in range(0, len(source_groups), 500):
            chunk = source_groups[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            self.connection.execute(
                f"UPDATE image_hashes SET group_id = ? WHERE group_id IN ({placeholders})",
                (destination, *chunk),
            )
        if commit:
            self.connection.commit()

    def commit(self) -> None:
        self.connection.commit()

    def grouped_paths(self) -> list[list[str]]:
        grouped: dict[int, list[str]] = defaultdict(list)
        for group_id, path in self.connection.execute("SELECT group_id, path FROM image_hashes ORDER BY group_id, path"):
            grouped[group_id].append(path)
        return [paths for paths in grouped.values() if len(paths) > 1]

    def image_count(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM image_hashes").fetchone()[0]


def image_files(root: Path) -> Iterator[ImageFile]:
    """Recorre imágenes y obtiene los datos necesarios para validar la caché."""
    for current_root, _, files in os.walk(root):
        for filename in files:
            path = Path(current_root, filename)
            if path.suffix.lower() in IMAGE_EXTENSIONS:
                try:
                    metadata = path.stat()
                except OSError:
                    continue
                yield ImageFile(str(path), metadata.st_size, metadata.st_mtime_ns)


def batches(items: Iterable[ImageFile], size: int) -> Iterator[list[ImageFile]]:
    iterator = iter(items)
    while batch := list(islice(iterator, size)):
        yield batch


def hash_images(
    files: list[ImageFile],
    workers: int,
    progress_callback: Callable[[int, int, dict[str, int], dict[str, int]], None] | None = None,
) -> tuple[dict[str, int], list[dict[str, str]]]:
    """Calcula en paralelo solo los hashes que no estaban en la caché."""
    hashes: dict[str, int] = {}
    unreadable: list[dict[str, str]] = []
    batch_size = max(64, workers * 32)
    processed = 0
    total = len(files)
    if total == 0:
        return {}, []

    def store(results: Iterable[tuple[str, int | None, str | None]]) -> dict[str, int]:
        batch_hashes: dict[str, int] = {}
        for path_as_text, image_hash, error in results:
            if image_hash is None:
                unreadable.append({"ruta": path_as_text, "error": error or "Error desconocido"})
            else:
                hashes[path_as_text] = image_hash
                batch_hashes[path_as_text] = image_hash
        return batch_hashes

    def report_progress(batch_size: int, batch_hashes: dict[str, int]) -> None:
        nonlocal processed
        processed += batch_size
        if progress_callback is not None:
            progress_callback(processed, total, hashes, batch_hashes)

    # Con un proceso se evita el coste de crear procesos hijos y facilita usarlo
    # también en entornos restringidos. Con más, la decodificación va en paralelo.
    if workers == 1:
        for batch in batches(files, batch_size):
            batch_hashes = store(map(perceptual_hash, (file.path for file in batch)))
            report_progress(len(batch), batch_hashes)
            print(f"Hasheadas nuevas/modificadas: {len(hashes):,}", end="\r", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for batch in batches(files, batch_size):
                # Agrupar tareas reduce mucho el tráfico IPC al procesar miles
                # de imágenes pequeñas.
                chunk_size = max(1, min(16, len(batch) // max(1, workers * 2)))
                batch_hashes = store(
                    pool.map(perceptual_hash, (file.path for file in batch), chunksize=chunk_size)
                )
                report_progress(len(batch), batch_hashes)
                print(f"Hasheadas nuevas/modificadas: {len(hashes):,}", end="\r", flush=True)
    print(" " * 40, end="\r")
    return hashes, unreadable


@dataclass(frozen=True)
class CacheSync:
    unreadable: list[dict[str, str]]
    calculated: int
    reused: int
    full_rebuild: bool
    new_images: int


@dataclass(frozen=True)
class AnalysisProgress:
    """Grupos ya disponibles para trabajar mientras continúa el análisis."""

    processed: int
    total: int
    groups: list[list[str]]
    groups_found: int


def load_images_from_cache(
    root: Path,
    cache: HashCache,
    workers: int,
    threshold: int,
    progress_callback: Callable[[AnalysisProgress], None] | None = None,
    known_files: Iterable[ImageFile] | None = None,
) -> CacheSync:
    """Sincroniza la caché y actualiza solo los grupos conectados por altas nuevas."""
    trusted_inventory = known_files is not None
    files = list(known_files) if known_files is not None else list(image_files(root))
    cached = cache.read_all()
    cached_failures = cache.read_unreadable()
    current_paths = {file.path for file in files}
    new_paths = {file.path for file in files if file.path not in cached}

    def changed(file: ImageFile, entry: CachedHash) -> bool:
        # Un inventario MTP reutilizado usa modified_ns=0: el archivo local no
        # se copió en este refresco, de modo que su hash sigue siendo válido
        # aunque un proveedor haya informado tamaños remotos inconsistentes.
        if trusted_inventory and file.modified_ns == 0:
            return False
        return entry.size != file.size or (
            entry.modified_ns != file.modified_ns
        )

    modified_paths = {
        file.path for file in files
        if (entry := cached.get(file.path)) is not None
        and changed(file, entry)
    }
    files_to_hash = [
        file for file in files
        if (entry := cached.get(file.path)) is None
        and (
            (failure := cached_failures.get(file.path)) is None
            or failure.size != file.size
            or (file.modified_ns != 0 and failure.modified_ns != file.modified_ns)
        )
        or entry is not None and changed(file, entry)
    ]
    # En una primera ejecución se agrupa cada lote conforme se termina de
    # hashear. Así la interfaz puede ofrecer grupos reales para revisar y
    # borrar sin esperar a que termine toda la carpeta.
    is_first_analysis = not cached
    streaming_grouper = StreamingGrouper(threshold) if is_first_analysis else None

    def report_hash_progress(
        processed: int,
        total: int,
        hashes: dict[str, int],
        batch_hashes: dict[str, int],
    ) -> None:
        if streaming_grouper is not None:
            streaming_grouper.add_batch(batch_hashes)
        if progress_callback is None:
            return
        if streaming_grouper is not None:
            groups = streaming_grouper.visible_groups()
            groups_found = streaming_grouper.group_count
        else:
            groups = []
            groups_found = 0
        progress_callback(AnalysisProgress(processed, total, groups, groups_found))

    calculated, newly_unreadable = hash_images(files_to_hash, workers, report_hash_progress)
    file_by_path = {file.path: file for file in files_to_hash}
    # La persona usuaria puede borrar una foto mientras se muestran los
    # grupos en directo. No guardamos en SQLite un hash de un archivo que ya
    # no exista o que haya cambiado durante el análisis.
    if trusted_inventory:
        calculated = {
            path: image_hash
            for path, image_hash in calculated.items()
            if Path(path).is_file()
        }
    else:
        calculated = {
            path: image_hash
            for path, image_hash in calculated.items()
            if (current := _current_file_metadata(path)) is not None
            and current == (file_by_path[path].size, file_by_path[path].modified_ns)
        }
    cache.save({path: (file_by_path[path], image_hash) for path, image_hash in calculated.items()})

    # Una imagen modificada que ya no se puede leer no debe conservar su hash antiguo.
    new_failure_errors = {item["ruta"]: item["error"] for item in newly_unreadable}
    cache.save_unreadable(
        {
            path: (file_by_path[path], error)
            for path, error in new_failure_errors.items()
            if path in file_by_path
        }
    )
    failed_paths = set(new_failure_errors)
    existing_paths = current_paths if trusted_inventory else {path for path in current_paths if Path(path).is_file()}
    removed_paths = set(cached) - existing_paths
    stale_paths = removed_paths | failed_paths
    cache.remove(stale_paths)
    removed_failure_paths = set(cached_failures) - existing_paths
    cache.remove_unreadable(set(calculated) | removed_failure_paths)

    skipped_unreadable = [
        {"ruta": file.path, "error": cached_failures[file.path].error}
        for file in files
        if file.path not in cached
        and (failure := cached_failures.get(file.path)) is not None
        and failure.size == file.size
        and (file.modified_ns == 0 or failure.modified_ns == file.modified_ns)
    ]
    unreadable = skipped_unreadable + newly_unreadable

    successful_new_paths = new_paths & set(calculated)
    cache.initialize_groups(successful_new_paths)
    full_rebuild = (
        bool(modified_paths)
        or bool(removed_paths)
        or cache.saved_threshold() != threshold
        or cache.has_uninitialized_groups()
    )
    if full_rebuild:
        records = cache.all_records()
        row_ids = [row_id for row_id, _, _ in records]
        if is_first_analysis and streaming_grouper is not None:
            row_position_by_path = {path: position for position, (_, path, _) in enumerate(records)}
            groups = [
                [row_position_by_path[path] for path in members if path in row_position_by_path]
                for members in streaming_grouper.all_groups()
            ]
            cache.replace_groups(row_ids, groups)
        else:
            hashes = [image_hash for _, _, image_hash in records]
            print("Reconstruyendo grupos completos por cambios, borrados o nuevo umbral...")
            cache.replace_groups(row_ids, group_similar_images(hashes, threshold))
    elif successful_new_paths:
        new_records = cache.records_for_paths(successful_new_paths)
        for position, (row_id, _, image_hash, current_group) in enumerate(new_records, start=1):
            connected_groups = cache.similar_group_ids(row_id, image_hash, threshold)
            destination = min({current_group, *connected_groups})
            cache.merge_groups(destination, connected_groups | {current_group}, commit=False)
            if position % 100 == 0 or position == len(new_records):
                print(f"Actualizando grupos de nuevas imágenes: {position:,}/{len(new_records):,}", end="\r", flush=True)
        cache.commit()
        print(" " * 70, end="\r")

    cache.set_threshold(threshold)
    reused = cache.image_count() - len(calculated)
    return CacheSync(unreadable, len(calculated), reused, full_rebuild, len(successful_new_paths))


class UnionFind:
    def __init__(self, count: int) -> None:
        self.parent = array("I", range(count))
        self.size = array("I", [1]) * count

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def add(self) -> int:
        item = len(self.parent)
        self.parent.append(item)
        self.size.append(1)
        return item

    def union(self, first: int, second: int) -> int:
        first_root, second_root = self.find(first), self.find(second)
        if first_root == second_root:
            return first_root
        if self.size[first_root] < self.size[second_root]:
            first_root, second_root = second_root, first_root
        self.parent[second_root] = first_root
        self.size[first_root] += self.size[second_root]
        return first_root


@dataclass(frozen=True)
class SearchPlan:
    widths: tuple[int, ...]
    local_radius: int


@lru_cache(maxsize=None)
def plan_for(max_distance: int) -> SearchPlan:
    """Plan exacto: toda pareja dentro de la distancia aparecerá como candidata.

    Se divide el hash en partes. Por el principio del palomar, si el total de
    bits distintos no supera el límite, alguna parte tendrá como mucho
    ``local_radius`` diferencias. Solo se consultan esas vecindades.
    """
    if max_distance == 0:
        return SearchPlan((64,), 0)
    if max_distance <= 3:
        return SearchPlan((16, 16, 16, 16), 0)
    if max_distance <= 6:
        return SearchPlan((21, 21, 22), 2)
    return SearchPlan((16, 16, 16, 16), 3)


@lru_cache(maxsize=None)
def masks(width: int, radius: int) -> tuple[int, ...]:
    result = [0]
    for changed_bits in range(1, radius + 1):
        result.extend(sum(1 << bit for bit in positions) for positions in combinations(range(width), changed_bits))
    return tuple(result)


def split_hash(image_hash: int, widths: tuple[int, ...]) -> tuple[int, ...]:
    values: list[int] = []
    bits_remaining = HASH_BITS
    for width in widths:
        bits_remaining -= width
        values.append((image_hash >> bits_remaining) & ((1 << width) - 1))
    return tuple(values)


def max_hamming_distance(threshold: int) -> int:
    return int(HASH_BITS * (100 - threshold) // 100)


def _current_file_metadata(path_as_text: str) -> tuple[int, int] | None:
    try:
        metadata = Path(path_as_text).stat()
    except OSError:
        return None
    return metadata.st_size, metadata.st_mtime_ns


class StreamingGrouper:
    """Índice incremental exacto para publicar grupos durante el hasheado."""

    def __init__(self, threshold: int) -> None:
        self.max_distance = max_hamming_distance(threshold)
        self.plan = plan_for(self.max_distance)
        self.per_part_masks = [masks(width, self.plan.local_radius) for width in self.plan.widths]
        self.index: list[dict[int, list[int]]] = [defaultdict(list) for _ in self.plan.widths]
        self.hashes: list[int] = []
        self.paths: list[str] = []
        self.groups = UnionFind(0)
        self.seen = array("I")
        self.marker = 0
        self.members: dict[int, list[str]] = {}
        self.grouped_roots: dict[int, None] = {}

    @property
    def group_count(self) -> int:
        return len(self.grouped_roots)

    def add_batch(self, hashes: dict[str, int]) -> None:
        for path, image_hash in hashes.items():
            self.add(path, image_hash)

    def add(self, path: str, image_hash: int) -> None:
        image_id = self.groups.add()
        self.hashes.append(image_hash)
        self.paths.append(path)
        self.seen.append(0)
        self.members[image_id] = [path]
        self.marker += 1

        for part, value in enumerate(split_hash(image_hash, self.plan.widths)):
            for mask in self.per_part_masks[part]:
                for candidate_id in self.index[part].get(value ^ mask, ()):
                    if self.seen[candidate_id] == self.marker:
                        continue
                    self.seen[candidate_id] = self.marker
                    if (image_hash ^ self.hashes[candidate_id]).bit_count() <= self.max_distance:
                        self._merge(image_id, candidate_id)

        for part, value in enumerate(split_hash(image_hash, self.plan.widths)):
            self.index[part][value].append(image_id)

    def _merge(self, first: int, second: int) -> None:
        first_root, second_root = self.groups.find(first), self.groups.find(second)
        if first_root == second_root:
            return
        destination = self.groups.union(first_root, second_root)
        source = second_root if destination == first_root else first_root
        destination_members = self.members.pop(destination)
        source_members = self.members.pop(source)
        self.grouped_roots.pop(destination, None)
        self.grouped_roots.pop(source, None)
        self.members[destination] = destination_members + source_members
        if len(self.members[destination]) > 1:
            self.grouped_roots[destination] = None

    def visible_groups(self) -> list[list[str]]:
        return [list(self.members[root]) for root in self.grouped_roots]

    def all_groups(self) -> list[list[str]]:
        return [list(self.members[root]) for root in self.grouped_roots]


def group_similar_images(
    hashes: list[int], threshold: int, show_progress: bool = True
) -> list[list[int]]:
    max_distance = max_hamming_distance(threshold)
    plan = plan_for(max_distance)
    per_part_masks = [masks(width, plan.local_radius) for width in plan.widths]
    index: list[dict[int, list[int]]] = [defaultdict(list) for _ in plan.widths]
    hash_parts = [split_hash(image_hash, plan.widths) for image_hash in hashes]

    for image_id, parts in enumerate(hash_parts):
        for part, value in enumerate(parts):
            index[part][value].append(image_id)

    groups = UnionFind(len(hashes))
    seen = array("I", [0]) * len(hashes)
    marker = 0
    comparisons = 0

    for image_id, image_hash in enumerate(hashes):
        marker += 1
        for part, value in enumerate(hash_parts[image_id]):
            for mask in per_part_masks[part]:
                for candidate_id in index[part].get(value ^ mask, ()):
                    if candidate_id <= image_id or seen[candidate_id] == marker:
                        continue
                    seen[candidate_id] = marker
                    comparisons += 1
                    if (image_hash ^ hashes[candidate_id]).bit_count() <= max_distance:
                        groups.union(image_id, candidate_id)
        if show_progress and (image_id % 500 == 0 or image_id + 1 == len(hashes)):
            print(f"Comparando: {image_id + 1:,}/{len(hashes):,} | candidatas verificadas: {comparisons:,}", end="\r", flush=True)
    if show_progress:
        print(" " * 100, end="\r")

    grouped: dict[int, list[int]] = defaultdict(list)
    for image_id in range(len(hashes)):
        grouped[groups.find(image_id)].append(image_id)
    return [members for members in grouped.values() if len(members) > 1]


@dataclass(frozen=True)
class AnalysisResult:
    root: Path
    threshold: int
    image_count: int
    groups: list[list[str]]
    sync: CacheSync


def analyze_folder(
    root: Path,
    threshold: int,
    workers: int,
    progress_callback: Callable[[AnalysisProgress], None] | None = None,
    known_files: Iterable[ImageFile] | None = None,
) -> AnalysisResult:
    """Ejecuta el análisis para la línea de comandos o la interfaz gráfica."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"La carpeta no existe o no es una carpeta: {root}")
    if threshold not in ALLOWED_THRESHOLDS:
        raise ValueError("La similitud debe ser 80, 90, 95 o 100")
    if workers < 1:
        raise ValueError("El número de procesos debe ser al menos 1")

    cache_path = root / ".similitud_imagenes.sqlite"
    try:
        cache = HashCache(cache_path)
        try:
            sync = load_images_from_cache(root, cache, workers, threshold, progress_callback, known_files)
            groups = cache.grouped_paths()
            image_count = cache.image_count()
        finally:
            cache.close()
    except (OSError, sqlite3.Error) as error:
        raise RuntimeError(f"No se pudo usar la caché {cache_path}: {error}") from error
    return AnalysisResult(root, threshold, image_count, groups, sync)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Agrupa imágenes de una carpeta y sus subcarpetas por similitud perceptual."
    )
    parser.add_argument("carpeta", type=Path, help="Carpeta raíz que se analizará.")
    parser.add_argument("--similitud", type=int, choices=ALLOWED_THRESHOLDS, default=90,
                        help="Mínimo de similitud: 80, 90, 95 o 100 (por defecto: 90).")
    parser.add_argument("--salida", type=Path, default=Path("grupos_similares.json"),
                        help="Archivo JSON resultante (por defecto: grupos_similares.json).")
    parser.add_argument("--procesos", type=int, default=os.cpu_count() or 1,
                        help="Procesos para leer imágenes (por defecto: núcleos de CPU).")
    parser.add_argument("--cache", type=Path,
                        help="Ruta del archivo SQLite de caché. Por defecto se guarda en la carpeta analizada.")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    root = args.carpeta.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"La carpeta no existe o no es una carpeta: {root}")
    if args.procesos < 1:
        raise SystemExit("--procesos debe ser al menos 1")
    print(f"Buscando imágenes en: {root}")
    print(f"Similitud mínima: {args.similitud}%")
    if args.cache:
        # La opción se conserva para la línea de comandos; la interfaz usa la
        # caché junto a la carpeta analizada para que sea autosuficiente.
        cache_path = args.cache.expanduser().resolve()
    else:
        cache_path = root / ".similitud_imagenes.sqlite"
    try:
        cache = HashCache(cache_path)
        try:
            sync = load_images_from_cache(root, cache, args.procesos, args.similitud)
            groups = cache.grouped_paths()
            image_count = cache.image_count()
        finally:
            cache.close()
    except (OSError, sqlite3.Error) as error:
        raise SystemExit(f"No se pudo usar la caché {cache_path}: {error}")

    print(f"Hashes reutilizados de la caché: {sync.reused:,}. Hashes calculados: {sync.calculated:,}.")
    print(f"Imágenes válidas: {image_count:,}.")

    result = {
        "carpeta_analizada": str(root),
        "similitud_minima_porcentaje": args.similitud,
        "imagenes_analizadas": image_count,
        "hashes_reutilizados_cache": sync.reused,
        "hashes_calculados_esta_ejecucion": sync.calculated,
        "actualizacion_incremental_grupos": not sync.full_rebuild,
        "imagenes_nuevas_agregadas_a_grupos": sync.new_images,
        "imagenes_no_leidas": sync.unreadable,
        "grupos": [
            {"id": position, "imagenes": members}
            for position, members in enumerate(groups, start=1)
        ],
    }
    output = args.salida.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    grouped_images = sum(len(group) for group in groups)
    print(f"Listo: {len(groups):,} grupos ({grouped_images:,} imágenes) en {output}")
    if sync.unreadable:
        print(f"No se pudieron leer {len(sync.unreadable):,} archivos; constan en el JSON.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
