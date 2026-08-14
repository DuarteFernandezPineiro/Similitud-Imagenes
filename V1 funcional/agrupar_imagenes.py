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
from itertools import combinations, islice
from pathlib import Path
from typing import Iterable, Iterator

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
        for path in paths:
            row = self.connection.execute(
                "SELECT rowid, path, value, group_id FROM image_hashes WHERE path = ?", (path,)
            ).fetchone()
            if row:
                result.append((row[0], row[1], int.from_bytes(row[2], "big"), row[3]))
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

    def merge_groups(self, destination: int, group_ids: Iterable[int]) -> None:
        """Fusiona únicamente los grupos que una foto nueva haya conectado."""
        source_groups = list(set(group_ids) - {destination})
        for start in range(0, len(source_groups), 500):
            chunk = source_groups[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            self.connection.execute(
                f"UPDATE image_hashes SET group_id = ? WHERE group_id IN ({placeholders})",
                (destination, *chunk),
            )
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


def hash_images(files: list[ImageFile], workers: int) -> tuple[dict[str, int], list[dict[str, str]]]:
    """Calcula en paralelo solo los hashes que no estaban en la caché."""
    hashes: dict[str, int] = {}
    unreadable: list[dict[str, str]] = []
    batch_size = max(64, workers * 32)

    def store(results: Iterable[tuple[str, int | None, str | None]]) -> None:
        for path_as_text, image_hash, error in results:
            if image_hash is None:
                unreadable.append({"ruta": path_as_text, "error": error or "Error desconocido"})
            else:
                hashes[path_as_text] = image_hash

    # Con un proceso se evita el coste de crear procesos hijos y facilita usarlo
    # también en entornos restringidos. Con más, la decodificación va en paralelo.
    if workers == 1:
        for batch in batches(files, batch_size):
            store(map(perceptual_hash, (file.path for file in batch)))
            print(f"Hasheadas nuevas/modificadas: {len(hashes):,}", end="\r", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for batch in batches(files, batch_size):
                store(pool.map(perceptual_hash, (file.path for file in batch)))
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


def load_images_from_cache(
    root: Path, cache: HashCache, workers: int, threshold: int
) -> CacheSync:
    """Sincroniza la caché y actualiza solo los grupos conectados por altas nuevas."""
    files = list(image_files(root))
    cached = cache.read_all()
    current_paths = {file.path for file in files}
    new_paths = {file.path for file in files if file.path not in cached}
    modified_paths = {
        file.path for file in files
        if (entry := cached.get(file.path)) is not None
        and (entry.size != file.size or entry.modified_ns != file.modified_ns)
    }
    files_to_hash = [
        file for file in files
        if (entry := cached.get(file.path)) is None
        or entry.size != file.size
        or entry.modified_ns != file.modified_ns
    ]
    calculated, unreadable = hash_images(files_to_hash, workers)
    file_by_path = {file.path: file for file in files_to_hash}
    cache.save({path: (file_by_path[path], image_hash) for path, image_hash in calculated.items()})

    # Una imagen modificada que ya no se puede leer no debe conservar su hash antiguo.
    failed_paths = {item["ruta"] for item in unreadable}
    removed_paths = set(cached) - current_paths
    stale_paths = removed_paths | failed_paths
    cache.remove(stale_paths)

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
        hashes = [image_hash for _, _, image_hash in records]
        print("Reconstruyendo grupos completos por cambios, borrados o nuevo umbral...")
        cache.replace_groups([row_id for row_id, _, _ in records], group_similar_images(hashes, threshold))
    elif successful_new_paths:
        new_records = cache.records_for_paths(successful_new_paths)
        for position, (row_id, _, image_hash, current_group) in enumerate(new_records, start=1):
            connected_groups = cache.similar_group_ids(row_id, image_hash, threshold)
            destination = min({current_group, *connected_groups})
            cache.merge_groups(destination, connected_groups | {current_group})
            if position % 100 == 0 or position == len(new_records):
                print(f"Actualizando grupos de nuevas imágenes: {position:,}/{len(new_records):,}", end="\r", flush=True)
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

    def union(self, first: int, second: int) -> None:
        first_root, second_root = self.find(first), self.find(second)
        if first_root == second_root:
            return
        if self.size[first_root] < self.size[second_root]:
            first_root, second_root = second_root, first_root
        self.parent[second_root] = first_root
        self.size[first_root] += self.size[second_root]


@dataclass(frozen=True)
class SearchPlan:
    widths: tuple[int, ...]
    local_radius: int


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


def group_similar_images(hashes: list[int], threshold: int) -> list[list[int]]:
    max_distance = max_hamming_distance(threshold)
    plan = plan_for(max_distance)
    per_part_masks = [masks(width, plan.local_radius) for width in plan.widths]
    index: list[dict[int, list[int]]] = [defaultdict(list) for _ in plan.widths]

    for image_id, image_hash in enumerate(hashes):
        for part, value in enumerate(split_hash(image_hash, plan.widths)):
            index[part][value].append(image_id)

    groups = UnionFind(len(hashes))
    seen = array("I", [0]) * len(hashes)
    marker = 0
    comparisons = 0

    for image_id, image_hash in enumerate(hashes):
        marker += 1
        for part, value in enumerate(split_hash(image_hash, plan.widths)):
            for mask in per_part_masks[part]:
                for candidate_id in index[part].get(value ^ mask, ()):
                    if candidate_id <= image_id or seen[candidate_id] == marker:
                        continue
                    seen[candidate_id] = marker
                    comparisons += 1
                    if (image_hash ^ hashes[candidate_id]).bit_count() <= max_distance:
                        groups.union(image_id, candidate_id)
        if image_id % 500 == 0 or image_id + 1 == len(hashes):
            print(f"Comparando: {image_id + 1:,}/{len(hashes):,} | candidatas verificadas: {comparisons:,}", end="\r", flush=True)
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


def analyze_folder(root: Path, threshold: int, workers: int) -> AnalysisResult:
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
            sync = load_images_from_cache(root, cache, workers, threshold)
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
