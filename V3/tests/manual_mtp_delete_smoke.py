"""Crea y elimina una imagen de prueba única en el móvil conectado."""

from __future__ import annotations

import json
import argparse
import sys
import tempfile
import time
import uuid
from pathlib import Path

from PIL import Image


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from movil_windows import (  # noqa: E402
    MobileImage,
    _json_rows,
    _powershell_string,
    _run_powershell,
    connected_devices,
    delete_mobile_images,
)


def cleanup_previous_tests(device) -> int:
    script = f"""
    $ErrorActionPreference = 'Stop'
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $shell = New-Object -ComObject Shell.Application
    $root = $shell.NameSpace({_powershell_string(device.shell_path)})
    $storageItem = @($root.Items() | Where-Object {{ $_.IsFolder }} | Select-Object -First 1)[0]
    $storage = $storageItem.GetFolder
    $picturesItem = @($storage.Items() | Where-Object {{ $_.IsFolder -and $_.Name -eq 'Pictures' }} | Select-Object -First 1)[0]
    $pictures = $picturesItem.GetFolder
    @($pictures.Items() | Where-Object {{ $_.Name -like '__similitud_v3_prueba_*.jpg' }}) | ForEach-Object {{
        [PSCustomObject]@{{
            relative = ([string]$storageItem.Name + '\\' + [string]$picturesItem.Name)
            name = [string]$_.Name
        }} | ConvertTo-Json -Compress
    }}
    """
    result = _run_powershell(script)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "No se pudieron localizar pruebas anteriores.")
    leftovers = [
        MobileImage("", device.shell_path, str(row["relative"]), str(row["name"]))
        for row in _json_rows(result.stdout)
    ]
    if not leftovers:
        return 0
    deleted, failures = delete_mobile_images(leftovers)
    if deleted != set(leftovers):
        raise RuntimeError("No se pudieron limpiar pruebas anteriores: " + "; ".join(failures))
    return len(deleted)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1)
    arguments = parser.parse_args()
    if not 1 <= arguments.count <= 20:
        parser.error("--count debe estar entre 1 y 20")
    devices = connected_devices()
    if not devices:
        print("No hay ningún dispositivo MTP conectado.")
        return 2
    device = devices[0]
    cleaned = cleanup_previous_tests(device)
    if cleaned:
        print(f"LIMPIEZA pruebas anteriores={cleaned}", flush=True)
    filenames = [
        f"__similitud_v3_prueba_{uuid.uuid4().hex}.jpg"
        for _ in range(arguments.count)
    ]
    with tempfile.TemporaryDirectory() as directory:
        local_paths = [Path(directory) / filename for filename in filenames]
        for local in local_paths:
            Image.new("RGB", (24, 24), "#2563eb").save(local, "JPEG")
        powershell_names = ",".join(_powershell_string(name) for name in filenames)
        script = f"""
        $ErrorActionPreference = 'Stop'
        [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
        $shell = New-Object -ComObject Shell.Application
        $root = $shell.NameSpace({_powershell_string(device.shell_path)})
        if ($null -eq $root) {{ throw 'No se pudo abrir el dispositivo.' }}
        $storageItem = @($root.Items() | Where-Object {{ $_.IsFolder }} | Select-Object -First 1)[0]
        if ($null -eq $storageItem) {{ throw 'No se encontró el almacenamiento del dispositivo.' }}
        $storage = $storageItem.GetFolder
        $picturesItem = @($storage.Items() | Where-Object {{ $_.IsFolder -and $_.Name -eq 'Pictures' }} | Select-Object -First 1)[0]
        if ($null -eq $picturesItem) {{ throw 'No se encontró Pictures en el dispositivo.' }}
        $pictures = $picturesItem.GetFolder
        $source = $shell.NameSpace({_powershell_string(str(local.parent))})
        $names = @({powershell_names})
        foreach ($name in $names) {{
            $sourceItem = $source.ParseName([string]$name)
            $pictures.CopyHere($sourceItem, 20)
        }}
        $copied = 0
        for ($attempt = 0; $attempt -lt 100; $attempt++) {{
            Start-Sleep -Milliseconds 100
            $copied = @($names | Where-Object {{ $null -ne $pictures.ParseName([string]$_) }}).Count
            if ($copied -eq $names.Count) {{ break }}
        }}
        if ($copied -ne $names.Count) {{ throw "Solo se copiaron $copied de $($names.Count) imágenes de prueba." }}
        [PSCustomObject]@{{
            storage = [string]$storageItem.Name
            relative = ([string]$storageItem.Name + '\\' + [string]$picturesItem.Name)
            count = $names.Count
        }} | ConvertTo-Json -Compress
        """
        result = _run_powershell(script)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "No se pudo crear el archivo de prueba.")
        row = json.loads(result.stdout.strip().splitlines()[-1])
        images = [
            MobileImage(str(local), device.shell_path, row["relative"], local.name, local.stat().st_size)
            for local in local_paths
        ]
        started = time.perf_counter()
        deleted, failures = delete_mobile_images(
            images,
            lambda position, total, name: print(f"PROGRESO {position}/{total} {name}", flush=True),
        )
        elapsed = time.perf_counter() - started
        if deleted != set(images):
            for remaining in set(images) - deleted:
                delete_mobile_images([remaining])
            raise RuntimeError("; ".join(failures) or "El móvil no confirmó el borrado de la prueba.")
        print(f"RESULTADO dispositivo={device.name} archivos={len(images)} borrado={elapsed:.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
