"""Prueba manual no destructiva del recorrido y la caché de un móvil MTP."""

from __future__ import annotations

import sys
import time
import argparse
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from agrupar_imagenes import ImageFile, analyze_folder  # noqa: E402
from movil_windows import MobileProgress, connected_devices, copy_images_from_device  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-only", action="store_true", help="No fuerza una nueva enumeración MTP.")
    arguments = parser.parse_args()
    devices = connected_devices()
    if not devices:
        print("No hay ningún dispositivo MTP conectado.", flush=True)
        return 2
    device = devices[0]
    print(f"Dispositivo: {device.name}", flush=True)
    last_report = 0.0

    def progress(update: MobileProgress) -> None:
        nonlocal last_report
        now = time.monotonic()
        if now - last_report >= 2 or update.processed == update.total:
            print(
                f"{update.phase}: {update.processed}/{update.total} {update.detail}",
                flush=True,
            )
            last_report = now

    refresh_seconds = 0.0
    refreshed = None
    if not arguments.cache_only:
        started = time.perf_counter()
        cache, refreshed = copy_images_from_device(device, progress, force_refresh=True)
        refresh_seconds = time.perf_counter() - started
    started = time.perf_counter()
    reused_cache, reused = copy_images_from_device(device, progress)
    reuse_seconds = time.perf_counter() - started
    if refreshed is not None and (cache != reused_cache or set(refreshed) != set(reused)):
        raise RuntimeError("La ruta rápida no devolvió el mismo manifiesto que el refresco completo.")
    inventory = [ImageFile(path, source.size, source.revision) for path, source in reused.items()]
    started = time.perf_counter()
    result = analyze_folder(reused_cache, 90, 8, known_files=inventory)
    analysis_seconds = time.perf_counter() - started
    print(
        f"RESULTADO fotos={len(reused)} refresco={refresh_seconds:.3f}s "
        f"cache={reuse_seconds:.3f}s analisis={analysis_seconds:.3f}s "
        f"hashes_nuevos={result.sync.calculated}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
