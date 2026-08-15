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
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


IMAGE_EXTENSIONS = {
    ".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".jxl",
    ".png", ".tif", ".tiff", ".webp",
}
CACHE_MARKER = ".similitud_movil_cache"
MANIFEST_FILE = ".galeria_movil.json"
MANIFEST_VERSION = 2
SHELL_OPERATIONS_CSHARP = r"""
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

[ComImport]
[Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IShellItem
{
    [PreserveSig] int BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
    [PreserveSig] int GetParent(out IShellItem ppsi);
    [PreserveSig] int GetDisplayName(uint sigdnName, out IntPtr ppszName);
    [PreserveSig] int GetAttributes(uint sfgaoMask, out uint psfgaoAttribs);
    [PreserveSig] int Compare(IShellItem psi, uint hint, out int piOrder);
}

[ComImport]
[Guid("947AAB5F-0A5C-4C13-B4D6-4BF7836FC9F8")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IFileOperation
{
    [PreserveSig] int Advise(IntPtr pfops, out uint cookie);
    [PreserveSig] int Unadvise(uint cookie);
    [PreserveSig] int SetOperationFlags(uint flags);
    [PreserveSig] int SetProgressMessage([MarshalAs(UnmanagedType.LPWStr)] string message);
    [PreserveSig] int SetProgressDialog(IntPtr popd);
    [PreserveSig] int SetProperties(IntPtr pproparray);
    [PreserveSig] int SetOwnerWindow(uint hwndOwner);
    [PreserveSig] int ApplyPropertiesToItem(IShellItem item);
    [PreserveSig] int ApplyPropertiesToItems(IntPtr items);
    [PreserveSig] int RenameItem(IShellItem item, [MarshalAs(UnmanagedType.LPWStr)] string newName, IntPtr sink);
    [PreserveSig] int RenameItems(IntPtr items, [MarshalAs(UnmanagedType.LPWStr)] string newName);
    [PreserveSig] int MoveItem(IShellItem item, IShellItem destination, [MarshalAs(UnmanagedType.LPWStr)] string newName, IntPtr sink);
    [PreserveSig] int MoveItems(IntPtr items, IShellItem destination);
    [PreserveSig] int CopyItem(IShellItem item, IShellItem destination, [MarshalAs(UnmanagedType.LPWStr)] string copyName, IntPtr sink);
    [PreserveSig] int CopyItems(IntPtr items, IShellItem destination);
    [PreserveSig] int DeleteItem(IShellItem item, IntPtr sink);
    [PreserveSig] int DeleteItems(IntPtr items);
    [PreserveSig] int NewItem(IShellItem destination, uint attributes, [MarshalAs(UnmanagedType.LPWStr)] string name, [MarshalAs(UnmanagedType.LPWStr)] string templateName, IntPtr sink);
    [PreserveSig] int PerformOperations();
    [PreserveSig] int GetAnyOperationsAborted([MarshalAs(UnmanagedType.Bool)] out bool aborted);
}

[ComImport]
[Guid("3AD05575-8857-4850-9277-11B85BDB8E09")]
public class FileOperationComObject { }

public static class MobileShellBatch
{
    private const uint FOF_SILENT = 0x0004;
    private const uint FOF_NOCONFIRMATION = 0x0010;
    private const uint FOF_NOERRORUI = 0x0400;
    private const uint FOF_NO_CONNECTED_ELEMENTS = 0x2000;

    [DllImport("shell32.dll")]
    private static extern int SHGetIDListFromObject(
        [MarshalAs(UnmanagedType.IUnknown)] object source, out IntPtr pidl);

    [DllImport("shell32.dll")]
    private static extern int SHCreateItemFromIDList(
        IntPtr pidl, ref Guid riid,
        [MarshalAs(UnmanagedType.Interface)] out IShellItem shellItem);

    private static void Check(int result)
    {
        if (result < 0) Marshal.ThrowExceptionForHR(result);
    }

    public static bool Delete(object[] sourceItems, out string error)
    {
        error = String.Empty;
        IFileOperation operation = null;
        var shellItems = new List<IShellItem>();
        try
        {
            operation = (IFileOperation)new FileOperationComObject();
            Check(operation.SetOperationFlags(
                FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_NO_CONNECTED_ELEMENTS));
            Guid shellItemId = typeof(IShellItem).GUID;
            foreach (object source in sourceItems)
            {
                IntPtr pidl = IntPtr.Zero;
                try
                {
                    Check(SHGetIDListFromObject(source, out pidl));
                    IShellItem item;
                    Check(SHCreateItemFromIDList(pidl, ref shellItemId, out item));
                    shellItems.Add(item);
                    Check(operation.DeleteItem(item, IntPtr.Zero));
                }
                finally
                {
                    if (pidl != IntPtr.Zero) Marshal.FreeCoTaskMem(pidl);
                }
            }
            Check(operation.PerformOperations());
            bool aborted;
            Check(operation.GetAnyOperationsAborted(out aborted));
            return !aborted;
        }
        catch (Exception exception)
        {
            error = exception.Message;
            return false;
        }
        finally
        {
            foreach (IShellItem item in shellItems)
                if (item != null && Marshal.IsComObject(item)) Marshal.FinalReleaseComObject(item);
            if (operation != null && Marshal.IsComObject(operation)) Marshal.FinalReleaseComObject(operation);
        }
    }
}

public static class MobileDeleteDialogCloser
{
    private const uint BM_CLICK = 0x00F5;
    private const int IDYES = 6;

    private delegate bool EnumWindowsProc(IntPtr window, IntPtr parameter);

    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr parameter);
    [DllImport("user32.dll")]
    private static extern bool EnumChildWindows(IntPtr parent, EnumWindowsProc callback, IntPtr parameter);
    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetWindowText(IntPtr window, StringBuilder text, int maxCount);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetWindowTextLength(IntPtr window);
    [DllImport("user32.dll")]
    private static extern IntPtr GetDlgItem(IntPtr window, int itemId);
    [DllImport("user32.dll")]
    private static extern IntPtr SendMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);

    private static string ReadText(IntPtr window)
    {
        int length = GetWindowTextLength(window);
        if (length == 0) return String.Empty;
        var text = new StringBuilder(length + 1);
        GetWindowText(window, text, text.Capacity);
        return text.ToString();
    }

    private static bool ContainsSelectedName(IntPtr window, string[] names)
    {
        if (Matches(ReadText(window), names)) return true;
        bool found = false;
        EnumChildWindows(window, delegate(IntPtr child, IntPtr ignored) {
            if (Matches(ReadText(child), names)) found = true;
            return !found;
        }, IntPtr.Zero);
        return found;
    }

    private static bool Matches(string text, string[] names)
    {
        if (String.IsNullOrEmpty(text)) return false;
        foreach (string name in names)
        {
            if (!String.IsNullOrEmpty(name) &&
                text.IndexOf(name, StringComparison.OrdinalIgnoreCase) >= 0)
                return true;
        }
        return false;
    }

    public static void Start(string[] names, int timeoutMilliseconds)
    {
        ThreadPool.QueueUserWorkItem(delegate {
            DateTime until = DateTime.UtcNow.AddMilliseconds(timeoutMilliseconds);
            while (DateTime.UtcNow < until)
            {
                EnumWindows(delegate(IntPtr window, IntPtr ignored) {
                    if (!IsWindowVisible(window)) return true;
                    string title = ReadText(window);
                    bool isDeleteConfirmation =
                        title.IndexOf("Confirmar la eliminación del archivo", StringComparison.OrdinalIgnoreCase) >= 0 ||
                        title.IndexOf("Confirm File Delete", StringComparison.OrdinalIgnoreCase) >= 0;
                    if (isDeleteConfirmation && ContainsSelectedName(window, names))
                    {
                        IntPtr yes = GetDlgItem(window, IDYES);
                        if (yes != IntPtr.Zero)
                            SendMessage(yes, BM_CLICK, IntPtr.Zero, IntPtr.Zero);
                    }
                    return true;
                }, IntPtr.Zero);
                Thread.Sleep(40);
            }
        });
    }
}
"""


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
    size: int = 0
    revision: int = 0


@dataclass(frozen=True)
class MobileProgress:
    phase: str
    processed: int
    total: int
    detail: str = ""


def _powershell_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _absolute_path(value: str | os.PathLike[str]) -> str:
    """Normaliza una ruta local sin la costosa resolución física de cada archivo."""
    return os.path.abspath(os.fspath(value))


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


def _start_powershell_script(script: str) -> tuple[subprocess.Popen[str], Path]:
    """Inicia un script grande sin superar el límite de la línea de comandos."""
    if os.name != "nt":
        raise RuntimeError("La integración con móviles por cable solo está disponible en Windows.")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8-sig",
        suffix=".ps1",
        prefix="similitud-mtp-",
        delete=False,
    ) as temporary:
        temporary.write(script)
        script_path = Path(temporary.name)
    try:
        process = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        script_path.unlink(missing_ok=True)
        raise
    return process, script_path


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


def _manifest_path(cache: Path) -> Path:
    return cache / MANIFEST_FILE


def _load_manifest(cache: Path, device: MobileDevice) -> dict[str, MobileImage]:
    """Carga solo manifiestos completos cuyas copias locales siguen disponibles."""
    try:
        payload = json.loads(_manifest_path(cache).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("version") != MANIFEST_VERSION or payload.get("device") != device.shell_path:
        return {}
    mapping: dict[str, MobileImage] = {}
    cache_root = Path(_absolute_path(cache))
    for row in payload.get("images", []):
        try:
            local_path = _absolute_path(str(row["local_path"]))
            local = Path(local_path)
            if not local.is_relative_to(cache_root):
                return {}
            size = int(row.get("size") or 0)
            mapping[local_path] = MobileImage(
                local_path,
                device.shell_path,
                str(row.get("relative") or ""),
                str(row["name"]),
                size,
                0,
            )
        except (KeyError, OSError, TypeError, ValueError):
            return {}
    # El manifiesto se escribe atómicamente después de un refresco completo.
    # Validar una muestra detecta una caché movida/borrada sin hacer 8.000 stats.
    paths = list(mapping)
    sample_step = max(1, len(paths) // 16)
    for path in paths[::sample_step]:
        try:
            local_size = Path(path).stat().st_size
        except OSError:
            return {}
        expected_size = mapping[path].size
        if local_size <= 0 or (expected_size > 0 and local_size != expected_size):
            return {}
    return mapping


def _save_manifest(cache: Path, device: MobileDevice, mapping: dict[str, MobileImage]) -> None:
    payload = {
        "version": MANIFEST_VERSION,
        "device": device.shell_path,
        "images": [
            {
                "local_path": image.local_path,
                "relative": image.relative_parent,
                "name": image.name,
                "size": image.size,
            }
            for image in mapping.values()
        ],
    }
    target = _manifest_path(cache)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, target)


def _remove_manifest_images(images: set[MobileImage]) -> None:
    """Evita que un borrado confirmado reaparezca al reutilizar la caché."""
    by_device: dict[str, set[tuple[str, str]]] = {}
    for image in images:
        by_device.setdefault(image.device_shell_path, set()).add((image.relative_parent, image.name))
    for shell_path, removed in by_device.items():
        device = MobileDevice("", shell_path)
        cache = _cache_directory(device)
        mapping = _load_manifest(cache, device)
        if not mapping:
            continue
        retained = {
            path: image
            for path, image in mapping.items()
            if (image.relative_parent, image.name) not in removed
        }
        try:
            _save_manifest(cache, device, retained)
        except OSError:
            pass


def _remove_stale_cache_images(
    cache: Path,
    current_paths: set[str],
    previous_paths: set[str] | None = None,
) -> None:
    """Retira solo copias de una caché que ya no existan en el móvil."""
    if not (cache / CACHE_MARKER).is_file():
        return
    cache_root = Path(_absolute_path(cache))
    candidates = (Path(path) for path in previous_paths) if previous_paths is not None else cache.rglob("*")
    for candidate in candidates:
        if candidate.suffix.casefold() not in IMAGE_EXTENSIONS:
            continue
        candidate_resolved = Path(_absolute_path(candidate))
        if not candidate_resolved.is_relative_to(cache_root):
            continue
        if str(candidate_resolved) in current_paths:
            continue
        try:
            candidate.unlink()
        except OSError:
            pass


def copy_images_from_device(
    device: MobileDevice,
    on_progress: Callable[[MobileProgress], None] | None = None,
    max_images: int | None = None,
    *,
    force_refresh: bool = False,
) -> tuple[Path, dict[str, MobileImage]]:
    """Materializa la galería pública del móvil en caché y devuelve su mapa remoto.

    MTP se bloquea con frecuencia en Android/data, Android/obb y carpetas
    ocultas. Se incluyen DCIM, Pictures, Download, Movies, Documents y
    Android/media, donde se almacenan los medios de apps modernas.
    """
    destination = _prepare_cache(device).resolve()
    previous_mapping = _load_manifest(destination, device)
    if previous_mapping and not force_refresh:
        mapping = previous_mapping
        if max_images is not None:
            mapping = dict(list(mapping.items())[:max_images])
        if on_progress:
            on_progress(MobileProgress("cache", len(mapping), len(mapping), "Caché local reutilizada"))
        return destination, mapping

    extension_values = ",".join(_powershell_string(item) for item in sorted(IMAGE_EXTENSIONS))
    script = f"""
    $ErrorActionPreference = 'Stop'
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $shell = New-Object -ComObject Shell.Application
    $devicePath = {_powershell_string(device.shell_path)}
    $root = $shell.NameSpace($devicePath)
    $destination = {_powershell_string(str(destination))}
    $extensions = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    @({extension_values}) | ForEach-Object {{ [void]$extensions.Add($_) }}
    $maxImages = {max_images or 0}
    $records = New-Object System.Collections.Generic.List[object]
    $visited = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $publicRoots = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    @('DCIM', 'Pictures', 'Movies', 'Download', 'Recordings', 'Documents',
      'fotos xoel', 'WhatsApp', 'Telegram', 'Camera', 'Screenshots',
      'Bluetooth', 'Android') | ForEach-Object {{ [void]$publicRoots.Add($_) }}
    $foldersScanned = 0
    $imagesFound = 0

    function Test-PublicFolder([string]$relative) {{
        $parts = @($relative -split '\\\\')
        # El primer componente suele ser Memoria interna o una tarjeta SD.
        if ($parts.Count -le 1) {{ return $true }}
        if ($parts[-1] -like '.*') {{ return $false }}
        if ($parts.Count -eq 2) {{ return $publicRoots.Contains($parts[1]) }}
        # Solo Android/media se expone con fiabilidad y contiene medios de apps.
        if ($parts[1] -eq 'Android' -and $parts.Count -eq 3) {{ return $parts[2] -eq 'media' }}
        if ($relative -match '(^|\\\\)Android\\\\(data|obb)(\\\\|$)') {{ return $false }}
        return $true
    }}

    function Visit-Folder([object]$folder, [string]$relative) {{
        $folderPath = [string]$folder.Self.Path
        if ([string]::IsNullOrWhiteSpace($folderPath) -or -not $visited.Add($folderPath)) {{ return }}
        try {{ $items = @($folder.Items()) }} catch {{ return }}
        $script:foldersScanned++
        if (($script:foldersScanned % 5) -eq 0) {{
            [PSCustomObject]@{{ event = 'scan'; folders = $script:foldersScanned; images = $script:imagesFound; relative = $relative }} | ConvertTo-Json -Compress
        }}
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
            if ($extensions.Contains($extension)) {{
                $remoteSize = 0
                try {{ $remoteSize = [int64]$item.Size }} catch {{}}
                $records.Add([PSCustomObject]@{{
                    item = $item
                    device = $devicePath
                    name = [string]$item.Name
                    relative = $relative
                    size = $remoteSize
                }})
                $script:imagesFound++
                if (($script:imagesFound % 250) -eq 0) {{
                    [PSCustomObject]@{{ event = 'scan'; folders = $script:foldersScanned; images = $script:imagesFound; relative = $relative }} | ConvertTo-Json -Compress
                }}
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
    $batchSize = 16
    $createdFolders = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    for ($start = 0; $start -lt $records.Count; $start += $batchSize) {{
        $pending = New-Object System.Collections.Generic.List[object]
        $end = [Math]::Min($start + $batchSize, $records.Count)
        for ($index = $start; $index -lt $end; $index++) {{
                $record = $records[$index]
            $position++
            try {{
                $targetFolder = if ([string]::IsNullOrWhiteSpace([string]$record.relative)) {{ $destination }} else {{ Join-Path $destination $record.relative }}
                if ($createdFolders.Add($targetFolder)) {{ [System.IO.Directory]::CreateDirectory($targetFolder) | Out-Null }}
                $target = Join-Path $targetFolder $record.name
                $localSize = if ([System.IO.File]::Exists($target)) {{ [System.IO.FileInfo]::new($target).Length }} else {{ 0 }}
                if ($localSize -gt 0 -and ([int64]$record.size -le 0 -or $localSize -eq [int64]$record.size)) {{
                    [PSCustomObject]@{{ event = 'file'; position = $position; total = $records.Count; local_path = $target; device = $record.device; relative = $record.relative; name = $record.name; size = $localSize; cached = $true }} | ConvertTo-Json -Compress
                    continue
                }}
                if (Test-Path -LiteralPath $target) {{ Remove-Item -LiteralPath $target -Force }}
                $targetShellFolder = $shell.NameSpace($targetFolder)
                if ($null -eq $targetShellFolder) {{ throw 'No se pudo crear la carpeta de caché local.' }}
                $targetShellFolder.CopyHere($record.item, 20)
                $pending.Add([PSCustomObject]@{{ record = $record; position = $position; target = $target }})
            }} catch {{
                [PSCustomObject]@{{ event = 'error'; position = $position; total = $records.Count; local_path = $target; name = $record.name; error = $_.Exception.Message }} | ConvertTo-Json -Compress
            }}
        }}
        for ($attempt = 0; $attempt -lt 240 -and $pending.Count -gt 0; $attempt++) {{
            Start-Sleep -Milliseconds 250
            foreach ($entry in $pending.ToArray()) {{
                if (-not (Test-Path -LiteralPath $entry.target)) {{ continue }}
                if ((Get-Item -LiteralPath $entry.target).Length -le 0) {{ continue }}
                $pending.Remove($entry)
                $copiedSize = (Get-Item -LiteralPath $entry.target).Length
                [PSCustomObject]@{{ event = 'file'; position = $entry.position; total = $records.Count; local_path = $entry.target; device = $entry.record.device; relative = $entry.record.relative; name = $entry.record.name; size = $copiedSize; cached = $false }} | ConvertTo-Json -Compress
            }}
        }}
        foreach ($entry in $pending) {{
            [PSCustomObject]@{{ event = 'error'; position = $entry.position; total = $records.Count; local_path = $entry.target; name = $entry.record.name; error = 'La copia no terminó en un minuto.' }} | ConvertTo-Json -Compress
        }}
    }}
    """
    process, script_path = _start_powershell_script(script)

    mapping: dict[str, MobileImage] = {}
    copy_errors: list[str] = []
    observed_paths: set[str] = set()
    stderr_chunks: list[str] = []
    assert process.stderr is not None
    stderr_reader = threading.Thread(
        target=lambda: stderr_chunks.append(process.stderr.read()),
        daemon=True,
        name="errores-mtp",
    )
    stderr_reader.start()
    assert process.stdout is not None
    for line in process.stdout:
        for row in _json_rows(line):
            event = row.get("event")
            if event == "scan" and on_progress:
                on_progress(
                    MobileProgress(
                        "scan",
                        int(row.get("images", 0)),
                        0,
                        str(row.get("relative") or "Galería del móvil"),
                    )
                )
            elif event == "count" and on_progress:
                on_progress(MobileProgress("prepare", 0, int(row.get("total", 0)), "Preparando caché"))
            elif event == "file":
                local_path = _absolute_path(str(row["local_path"]))
                observed_paths.add(local_path)
                revision = 0
                if not row.get("cached"):
                    try:
                        revision = Path(local_path).stat().st_mtime_ns
                    except OSError:
                        revision = 1
                mapping[local_path] = MobileImage(
                    local_path,
                    str(row["device"]),
                    str(row.get("relative") or ""),
                    str(row["name"]),
                    int(row.get("size") or 0),
                    revision,
                )
                if on_progress:
                    on_progress(
                        MobileProgress(
                            "reuse" if row.get("cached") else "copy",
                            int(row["position"]),
                            int(row["total"]),
                            str(row["name"]),
                        )
                    )
            elif event == "error":
                if row.get("local_path"):
                    observed_paths.add(_absolute_path(str(row["local_path"])))
                copy_errors.append(f"{row.get('name', 'Archivo')}: {row.get('error', 'No se pudo copiar')}")

    exit_code = process.wait()
    stderr_reader.join(timeout=2)
    script_path.unlink(missing_ok=True)
    stderr = "".join(stderr_chunks)
    if exit_code and not mapping:
        raise RuntimeError(stderr.strip() or "No se pudieron preparar las fotos del móvil.")
    if not mapping:
        detail = copy_errors[0] if copy_errors else "No se encontraron imágenes compatibles en el móvil."
        raise RuntimeError(f"No se pudieron preparar fotos del móvil: {detail}")
    if max_images is None:
        _remove_stale_cache_images(
            destination,
            observed_paths,
            set(previous_mapping) if previous_mapping else None,
        )
        if not copy_errors:
            _save_manifest(destination, device, mapping)
    return destination, mapping


def delete_mobile_images(
    images: list[MobileImage],
    on_progress: Callable[[int, int, str], None] | None = None,
) -> tuple[set[MobileImage], list[str]]:
    """Borra un lote MTP sin diálogos del Shell y verifica cada resultado.

    La vía principal usa una sola instancia de IFileOperation con
    FOF_NOCONFIRMATION. Solo si el proveedor MTP rechaza esa API se recurre al
    verbo clásico de Explorer, aceptando exclusivamente avisos de los nombres
    que la persona ya confirmó en la interfaz.
    """
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
    $records = @([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String({_powershell_string(encoded_rows)})) | ConvertFrom-Json)
    $shell = New-Object -ComObject Shell.Application
    $folders = @{{}}
    $candidates = New-Object System.Collections.Generic.List[object]
    $pending = New-Object System.Collections.Generic.List[object]

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

    function Get-FolderKey([object]$record) {{
        return ([string]$record.device + [char]0 + [string]$record.relative)
    }}

    function Get-CachedFolder([object]$record, [bool]$refresh) {{
        $key = Get-FolderKey $record
        if ($refresh -or -not $folders.ContainsKey($key)) {{
            $folders[$key] = Open-MobileFolder $record
        }}
        return $folders[$key]
    }}

    function Write-Result([object]$record, [bool]$deleted, [string]$errorMessage) {{
        [PSCustomObject]@{{
            device = $record.device
            relative = $record.relative
            name = $record.name
            deleted = $deleted
            error = $errorMessage
        }} | ConvertTo-Json -Compress
    }}

    function Confirm-Deleted([System.Collections.Generic.List[object]]$items, [int]$timeoutMilliseconds) {{
        $until = [DateTime]::UtcNow.AddMilliseconds($timeoutMilliseconds)
        while ($items.Count -gt 0 -and [DateTime]::UtcNow -lt $until) {{
            Start-Sleep -Milliseconds 200
            foreach ($group in @($items.ToArray() | Group-Object key)) {{
                try {{
                    $sample = $group.Group[0].record
                    $folder = Get-CachedFolder $sample $true
                    foreach ($entry in @($group.Group)) {{
                        if ($null -ne $folder.ParseName([string]$entry.record.name)) {{ continue }}
                        [void]$items.Remove($entry)
                        Write-Result $entry.record $true ''
                    }}
                }} catch {{}}
            }}
        }}
    }}

    $shellOperations = @'
{SHELL_OPERATIONS_CSHARP}
'@
    if (-not ('MobileShellBatch' -as [type])) {{
        Add-Type -TypeDefinition $shellOperations -ErrorAction Stop
    }}

    foreach ($record in $records) {{
        try {{
            $folder = Get-CachedFolder $record $false
            $item = $folder.ParseName([string]$record.name)
            if ($null -eq $item) {{ throw 'No se encontró el archivo en el móvil.' }}
            $entry = [PSCustomObject]@{{ record = $record; item = $item; key = (Get-FolderKey $record) }}
            $candidates.Add($entry)
            $pending.Add($entry)
        }} catch {{
            Write-Result $record $false $_.Exception.Message
        }}
    }}

    # IFileOperation recibe todos los IShellItem antes de ejecutar: Windows ve
    # una única operación y FOF_NOCONFIRMATION equivale a "Sí a todo".
    if ($candidates.Count -gt 0) {{
        $batchError = ''
        [void][MobileShellBatch]::Delete([object[]]@($candidates | ForEach-Object {{ $_.item }}), [ref]$batchError)
        Confirm-Deleted $pending 10000
    }}

    # Fallback para extensiones MTP defectuosas. Solo alcanza elementos que la
    # operación nativa no eliminó y el cerrador se restringe a sus nombres.
    if ($pending.Count -gt 0) {{
        [MobileDeleteDialogCloser]::Start(
            [string[]]@($pending | ForEach-Object {{ [string]$_.record.name }}),
            [Math]::Max(15000, $pending.Count * 1500)
        )
        foreach ($entry in $pending.ToArray()) {{
            try {{
                $folder = Get-CachedFolder $entry.record $true
                $item = $folder.ParseName([string]$entry.record.name)
                if ($null -eq $item) {{ continue }}
                try {{
                    $item.InvokeVerb('delete')
                }} catch {{
                    $deleteVerb = @($item.Verbs() | Where-Object {{
                        ([string]$_.Name -replace '&', '') -match '(?i)^(delete|eliminar)'
                    }} | Select-Object -First 1)
                    if ($deleteVerb.Count -eq 0) {{ throw }}
                    $item.InvokeVerb([string]$deleteVerb[0].Name)
                }}
            }} catch {{
                # La comprobación final determinará si el proveedor aceptó la operación.
            }}
        }}
        Confirm-Deleted $pending 30000
    }}
    foreach ($entry in $pending) {{
        Write-Result $entry.record $false 'Windows no confirmó el borrado en el móvil.'
    }}
    """
    process, script_path = _start_powershell_script(script)
    by_remote = {
        (image.device_shell_path, image.relative_parent, image.name): image
        for image in images
    }
    deleted: set[MobileImage] = set()
    failures: list[str] = []
    stderr_chunks: list[str] = []
    assert process.stderr is not None
    stderr_reader = threading.Thread(
        target=lambda: stderr_chunks.append(process.stderr.read()),
        daemon=True,
        name="errores-borrado-mtp",
    )
    stderr_reader.start()
    assert process.stdout is not None
    processed = 0
    for line in process.stdout:
        for row in _json_rows(line):
            key = (str(row.get("device")), str(row.get("relative") or ""), str(row.get("name")))
            image = by_remote.get(key)
            if image is None:
                continue
            processed += 1
            if row.get("deleted"):
                deleted.add(image)
            else:
                failures.append(f"{row.get('name', 'Archivo')}: {row.get('error', 'No se pudo borrar')}")
            if on_progress:
                on_progress(processed, len(images), image.name)
    exit_code = process.wait()
    stderr_reader.join(timeout=2)
    script_path.unlink(missing_ok=True)
    if exit_code and not failures:
        failures.append("".join(stderr_chunks).strip() or "Windows no pudo borrar los archivos del móvil.")
    if deleted:
        _remove_manifest_images(deleted)
    return deleted, failures
