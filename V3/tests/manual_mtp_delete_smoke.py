"""Crea y elimina una imagen de prueba única en el móvil conectado."""

from __future__ import annotations

import json
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
    _powershell_string,
    _run_powershell,
    connected_devices,
    delete_mobile_images,
)


def main() -> int:
    devices = connected_devices()
    if not devices:
        print("No hay ningún dispositivo MTP conectado.")
        return 2
    device = devices[0]
    filename = f"__similitud_v3_prueba_{uuid.uuid4().hex}.jpg"
    with tempfile.TemporaryDirectory() as directory:
        local = Path(directory) / filename
        Image.new("RGB", (24, 24), "#2563eb").save(local, "JPEG")
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
        $sourceItem = $source.ParseName({_powershell_string(filename)})
        $pictures.CopyHere($sourceItem, 20)
        $copied = $false
        for ($attempt = 0; $attempt -lt 100; $attempt++) {{
            Start-Sleep -Milliseconds 100
            if ($null -ne $pictures.ParseName({_powershell_string(filename)})) {{ $copied = $true; break }}
        }}
        if (-not $copied) {{ throw 'La imagen de prueba no terminó de copiarse.' }}
        [PSCustomObject]@{{
            storage = [string]$storageItem.Name
            relative = ([string]$storageItem.Name + '\\' + [string]$picturesItem.Name)
            name = {_powershell_string(filename)}
        }} | ConvertTo-Json -Compress
        """
        result = _run_powershell(script)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "No se pudo crear el archivo de prueba.")
        row = json.loads(result.stdout.strip().splitlines()[-1])
        image = MobileImage(str(local), device.shell_path, row["relative"], filename, local.stat().st_size)
        started = time.perf_counter()
        deleted, failures = delete_mobile_images([image])
        elapsed = time.perf_counter() - started
        if image not in deleted:
            raise RuntimeError("; ".join(failures) or "El móvil no confirmó el borrado de la prueba.")
        print(f"RESULTADO dispositivo={device.name} archivo={filename} borrado={elapsed:.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
