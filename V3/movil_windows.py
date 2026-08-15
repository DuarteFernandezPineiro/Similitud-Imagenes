"""Lectura fiable de imágenes de teléfonos MTP conectados a Windows.

El teléfono no es una unidad de disco: Windows lo expone mediante el Shell de
Explorer. Este módulo enumera las rutas multimedia públicas, copia las imágenes
a una caché local y conserva la referencia necesaria para borrar el original
solo cuando la persona lo confirma en la interfaz.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


IMAGE_EXTENSIONS = {
    ".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".jxl",
    ".png", ".tif", ".tiff", ".webp",
}
CACHE_MARKER = ".similitud_movil_cache"


@dataclass(frozen=True)
class MobileDevice:
    name: str
    shell_path: str


@dataclass(frozen=True)
class MobileImage:
    local_path: str
    device_shell_path: str
    relative_parent: str
    name: str


def _powershell_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    if os.name != "nt":
        raise RuntimeError("La lectura de móviles por cable solo está disponible en Windows.")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
    )


def _json_rows(text: str) -> Iterator[dict[str, object]]:
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield value


def connected_devices() -> list[MobileDevice]:
    """Devuelve los dispositivos MTP que aparecen en «Este equipo»."""
    result = _run_powershell(
        """
        $ErrorActionPreference = 'Stop'
        [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
        $shell = New-Object -ComObject Shell.Application
        $thisPc = $shell.NameSpace(17)
        if ($null -eq $thisPc) { throw 'No se pudo abrir Este equipo.' }
        $thisPc.Items() | Where-Object {
            $_.IsFolder -and -not $_.IsFileSystem
        } | ForEach-Object {
            [PSCustomObject]@{ name = $_.Name; shell_path = $_.Path } | ConvertTo-Json -Compress
        }
        """
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Windows no pudo buscar dispositivos conectados.")
    devices = [
        MobileDevice(str(row["name"]), str(row["shell_path"]))
        for row in _json_rows(result.stdout)
        if row.get("name") and row.get("shell_path")
    ]
    return sorted(devices, key=lambda device: device.name.casefold())


def _cache_directory(device: MobileDevice) -> Path:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    identity = hashlib.sha256(device.shell_path.encode("utf-8")).hexdigest()[:24]
    return local_app_data / "SimilitudImagenes" / "moviles" / identity


def _prepare_cache(device: MobileDevice) -> Path:
    cache = _cache_directory(device)
    marker = cache / CACHE_MARKER
    if cache.exists():
        if not marker.is_file():
            raise RuntimeError(f"La caché móvil no es reconocible y no se tocará: {cache}")
    else:
        cache.mkdir(parents=True, exist_ok=False)
        marker.write_text("Caché creada por Similitud de imágenes.\n", encoding="utf-8")
    return cache


def _remove_stale_cache_images(cache: Path, current_paths: set[str]) -> None:
    """Retira solo copias de una caché que ya no existan en el móvil."""
    if not (cache / CACHE_MARKER).is_file():
        return
    for candidate in cache.rglob("*"):
        if candidate.suffix.casefold() not in IMAGE_EXTENSIONS:
            continue
        if str(candidate.resolve()) in current_paths:
            continue
        try:
            candidate.unlink()
        except OSError:
            pass


def copy_images_from_device(
    device: MobileDevice,
    on_progress: Callable[[int, int, str], None] | None = None,
    max_images: int | None = None,
) -> tuple[Path, dict[str, MobileImage]]:
    """Materializa la galería pública del móvil en caché y devuelve su mapa remoto.

    MTP se bloquea con frecuencia en Android/data, Android/obb y carpetas
    ocultas. Se incluyen DCIM, Pictures, Download, Movies, Documents y
    Android/media, donde se almacenan los medios de apps modernas.
    """
    destination = _prepare_cache(device).resolve()
    extension_values = ",".join(_powershell_string(item) for item in sorted(IMAGE_EXTENSIONS))
    script = f"""
    $ErrorActionPreference = 'Stop'
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $shell = New-Object -ComObject Shell.Application
    $devicePath = {_powershell_string(device.shell_path)}
    $root = $shell.NameSpace($devicePath)
    $destination = {_powershell_string(str(destination))}
    $extensions = @({extension_values})
    $maxImages = {max_images or 0}
    $records = New-Object System.Collections.Generic.List[object]
    $visited = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)

    function Test-PublicFolder([string]$relative) {{
        $parts = @($relative -split '\\\\')
        # El primer componente suele ser Memoria interna o una tarjeta SD.
        if ($parts.Count -le 1) {{ return $true }}
        if ($parts[-1] -like '.*') {{ return $false }}
        $publicRoots = @(
            'DCIM', 'Pictures', 'Movies', 'Download', 'Recordings', 'Documents',
            'fotos xoel', 'WhatsApp', 'Telegram', 'Camera', 'Screenshots',
            'Bluetooth', 'Android'
        )
        if ($parts.Count -eq 2) {{ return $parts[1] -in $publicRoots }}
        # Solo Android/media se expone con fiabilidad y contiene medios de apps.
        if ($parts[1] -eq 'Android' -and $parts.Count -eq 3) {{ return $parts[2] -eq 'media' }}
        if ($relative -match '(^|\\\\)Android\\\\(data|obb)(\\\\|$)') {{ return $false }}
        return $true
    }}

    function Visit-Folder([object]$folder, [string]$relative) {{
        $folderPath = [string]$folder.Self.Path
        if ([string]::IsNullOrWhiteSpace($folderPath) -or -not $visited.Add($folderPath)) {{ return }}
        try {{ $items = @($folder.Items()) }} catch {{ return }}
        foreach ($item in $items) {{
            $childRelative = if ([string]::IsNullOrWhiteSpace($relative)) {{ [string]$item.Name }} else {{ "$relative\$($item.Name)" }}
            if ($item.IsFolder) {{
                if (-not (Test-PublicFolder $childRelative)) {{ continue }}
                try {{
                    $child = $item.GetFolder
                    if ($null -ne $child) {{ Visit-Folder $child $childRelative }}
                }} catch {{}}
                continue
            }}
            $extension = [System.IO.Path]::GetExtension([string]$item.Name).ToLowerInvariant()
            if ($extensions -contains $extension) {{
                $records.Add([PSCustomObject]@{{
                    item = $item
                    device = $devicePath
                    name = [string]$item.Name
                    relative = $relative
                }})
            }}
        }}
    }}

    if ($null -eq $root) {{ throw 'No se pudo abrir el móvil. Desbloquéalo y permite transferir archivos.' }}
    Visit-Folder $root ''
    if ($maxImages -gt 0 -and $records.Count -gt $maxImages) {{
        $records = [System.Collections.Generic.List[object]]@($records | Select-Object -First $maxImages)
    }}
    [PSCustomObject]@{{ event = 'count'; total = $records.Count }} | ConvertTo-Json -Compress

    $position = 0
    $batchSize = 8
    for ($start = 0; $start -lt $records.Count; $start += $batchSize) {{
        $pending = New-Object System.Collections.Generic.List[object]
        $end = [Math]::Min($start + $batchSize, $records.Count)
        for ($index = $start; $index -lt $end; $index++) {{
            $record = $records[$index]
            $position++
            try {{
                $targetFolder = if ([string]::IsNullOrWhiteSpace([string]$record.relative)) {{ $destination }} else {{ Join-Path $destination $record.relative }}
                [System.IO.Directory]::CreateDirectory($targetFolder) | Out-Null
                $target = Join-Path $targetFolder $record.name
                if ((Test-Path -LiteralPath $target) -and (Get-Item -LiteralPath $target).Length -gt 0) {{
                    [PSCustomObject]@{{ event = 'file'; position = $position; total = $records.Count; local_path = $target; device = $record.device; relative = $record.relative; name = $record.name }} | ConvertTo-Json -Compress
                    continue
                }}
                if (Test-Path -LiteralPath $target) {{ Remove-Item -LiteralPath $target -Force }}
                $targetShellFolder = $shell.NameSpace($targetFolder)
                if ($null -eq $targetShellFolder) {{ throw 'No se pudo crear la carpeta de caché local.' }}
                $targetShellFolder.CopyHere($record.item, 20)
                $pending.Add([PSCustomObject]@{{ record = $record; position = $position; target = $target }})
            }} catch {{
                [PSCustomObject]@{{ event = 'error'; position = $position; total = $records.Count; name = $record.name; error = $_.Exception.Message }} | ConvertTo-Json -Compress
            }}
        }}
        for ($attempt = 0; $attempt -lt 240 -and $pending.Count -gt 0; $attempt++) {{
            Start-Sleep -Milliseconds 250
            foreach ($entry in $pending.ToArray()) {{
                if (-not (Test-Path -LiteralPath $entry.target)) {{ continue }}
                if ((Get-Item -LiteralPath $entry.target).Length -le 0) {{ continue }}
                $pending.Remove($entry)
                [PSCustomObject]@{{ event = 'file'; position = $entry.position; total = $records.Count; local_path = $entry.target; device = $entry.record.device; relative = $entry.record.relative; name = $entry.record.name }} | ConvertTo-Json -Compress
            }}
        }}
        foreach ($entry in $pending) {{
            [PSCustomObject]@{{ event = 'error'; position = $entry.position; total = $records.Count; name = $entry.record.name; error = 'La copia no terminó en un minuto.' }} | ConvertTo-Json -Compress
        }}
    }}
    """
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    process = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    mapping: dict[str, MobileImage] = {}
    copy_errors: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        for row in _json_rows(line):
            event = row.get("event")
            if event == "count" and on_progress:
                on_progress(0, int(row.get("total", 0)), "Fotos localizadas; preparando caché…")
            elif event == "file":
                local_path = str(Path(str(row["local_path"])).resolve())
                mapping[local_path] = MobileImage(
                    local_path,
                    str(row["device"]),
                    str(row.get("relative") or ""),
                    str(row["name"]),
                )
                if on_progress:
                    on_progress(int(row["position"]), int(row["total"]), str(row["name"]))
            elif event == "error":
                copy_errors.append(f"{row.get('name', 'Archivo')}: {row.get('error', 'No se pudo copiar')}")

    stderr = process.stderr.read() if process.stderr else ""
    exit_code = process.wait()
    if exit_code and not mapping:
        raise RuntimeError(stderr.strip() or "No se pudieron preparar las fotos del móvil.")
    if not mapping:
        detail = copy_errors[0] if copy_errors else "No se encontraron imágenes compatibles en el móvil."
        raise RuntimeError(f"No se pudieron preparar fotos del móvil: {detail}")
    _remove_stale_cache_images(destination, set(mapping))
    return destination, mapping


def delete_mobile_images(images: list[MobileImage]) -> tuple[set[MobileImage], list[str]]:
    """Borra originales MTP y solo confirma éxito cuando desaparecen del móvil."""
    if not images:
        return set(), []
    rows = [
        {"device": image.device_shell_path, "relative": image.relative_parent, "name": image.name}
        for image in images
    ]
    encoded_rows = base64.b64encode(json.dumps(rows).encode("utf-8")).decode("ascii")
    script = f"""
    $ErrorActionPreference = 'Continue'
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $records = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String({_powershell_string(encoded_rows)})) | ConvertFrom-Json
    $shell = New-Object -ComObject Shell.Application
    $pending = New-Object System.Collections.Generic.List[object]
    $confirmationKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Policies\Explorer'
    $confirmationName = 'ConfirmFileDelete'
    $keyExisted = Test-Path -LiteralPath $confirmationKey
    $hadConfirmationValue = $false
    $previousConfirmationValue = $null

    function Open-MobileFolder([object]$record) {{
        # La ruta Self.Path de una carpeta MTP solo es válida para la sesión que
        # la enumeró. Se vuelve a abrir desde la raíz del dispositivo usando la
        # ruta relativa estable guardada durante la importación.
        $folder = $shell.NameSpace([string]$record.device)
        if ($null -eq $folder) {{ throw 'No se pudo abrir el dispositivo conectado.' }}
        foreach ($segment in ([string]$record.relative -split '\\\\')) {{
            if ([string]::IsNullOrWhiteSpace($segment)) {{ continue }}
            $folderItem = $folder.ParseName($segment)
            if ($null -eq $folderItem) {{ throw "No se pudo abrir la carpeta '$segment' en el móvil." }}
            $folder = $folderItem.GetFolder
            if ($null -eq $folder) {{ throw "No se pudo abrir la carpeta '$segment' en el móvil." }}
        }}
        return $folder
    }}

    # La aplicación ya muestra una única confirmación para toda la selección.
    # Este ajuste de Explorer se desactiva de forma temporal para que InvokeVerb
    # no abra un cuadro adicional por cada archivo MTP y se restaura enseguida.
    try {{
        try {{
            $existing = Get-ItemProperty -LiteralPath $confirmationKey -Name $confirmationName -ErrorAction SilentlyContinue
            if ($null -ne $existing) {{
                $hadConfirmationValue = $true
                $previousConfirmationValue = $existing.$confirmationName
            }}
            if (-not $keyExisted) {{ New-Item -Path $confirmationKey -Force | Out-Null }}
            New-ItemProperty -LiteralPath $confirmationKey -Name $confirmationName -PropertyType DWord -Value 0 -Force | Out-Null
        }} catch {{
            # Si una política corporativa impide cambiarlo, se conserva el
            # comportamiento de Windows en lugar de bloquear el borrado.
        }}

        foreach ($record in $records) {{
            try {{
                $folder = Open-MobileFolder $record
                $item = $folder.ParseName([string]$record.name)
                if ($null -eq $item) {{ throw 'No se encontró el archivo en el móvil.' }}
                # "delete" es el verbo canónico del Shell. Si un proveedor MTP no
                # lo publica así, se busca el verbo visible (también en Windows en
                # español) antes de dar el archivo por no eliminable.
                try {{
                    $item.InvokeVerb('delete')
                }} catch {{
                    $deleteVerb = @($item.Verbs() | Where-Object {{
                        ([string]$_.Name -replace '&', '') -match '(?i)^(delete|eliminar)'
                    }} | Select-Object -First 1)
                    if ($deleteVerb.Count -eq 0) {{ throw }}
                    $item.InvokeVerb([string]$deleteVerb[0].Name)
                }}
                $pending.Add($record)
            }} catch {{
                [PSCustomObject]@{{ device = $record.device; relative = $record.relative; name = $record.name; deleted = $false; error = $_.Exception.Message }} | ConvertTo-Json -Compress
            }}
        }}
    }} finally {{
        try {{
            if ($hadConfirmationValue) {{
                New-ItemProperty -LiteralPath $confirmationKey -Name $confirmationName -PropertyType DWord -Value $previousConfirmationValue -Force | Out-Null
            }} else {{
                Remove-ItemProperty -LiteralPath $confirmationKey -Name $confirmationName -ErrorAction SilentlyContinue
                if (-not $keyExisted) {{ Remove-Item -LiteralPath $confirmationKey -Force -ErrorAction SilentlyContinue }}
            }}
        }} catch {{}}
    }}

    # InvokeVerb inicia una operación asíncrona para MTP. No se borra la copia
    # local ni se actualiza la interfaz hasta que una consulta nueva al Shell
    # confirme que el elemento ya no está en el dispositivo.
    for ($attempt = 0; $attempt -lt 120 -and $pending.Count -gt 0; $attempt++) {{
        Start-Sleep -Milliseconds 250
        foreach ($record in $pending.ToArray()) {{
            try {{
                $checkFolder = Open-MobileFolder $record
                $remaining = $checkFolder.ParseName([string]$record.name)
                if ($null -ne $remaining) {{ continue }}
                $pending.Remove($record)
                [PSCustomObject]@{{ device = $record.device; relative = $record.relative; name = $record.name; deleted = $true }} | ConvertTo-Json -Compress
            }} catch {{
                # Un error temporal de MTP no equivale a que se haya borrado.
            }}
        }}
    }}
    foreach ($record in $pending) {{
        [PSCustomObject]@{{ device = $record.device; relative = $record.relative; name = $record.name; deleted = $false; error = 'Windows no confirmó el borrado en el móvil.' }} | ConvertTo-Json -Compress
    }}
    """
    result = _run_powershell(script)
    by_remote = {
        (image.device_shell_path, image.relative_parent, image.name): image
        for image in images
    }
    deleted = {
        by_remote[(str(row.get("device")), str(row.get("relative") or ""), str(row.get("name")))]
        for row in _json_rows(result.stdout)
        if row.get("deleted")
        and (str(row.get("device")), str(row.get("relative") or ""), str(row.get("name"))) in by_remote
    }
    failures = [
        f"{row.get('name', 'Archivo')}: {row.get('error', 'No se pudo borrar')}"
        for row in _json_rows(result.stdout)
        if not row.get("deleted")
    ]
    if result.returncode and not failures:
        failures.append(result.stderr.strip() or "Windows no pudo borrar los archivos del móvil.")
    return deleted, failures
