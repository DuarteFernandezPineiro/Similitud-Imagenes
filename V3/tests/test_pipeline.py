from __future__ import annotations

import random
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from agrupar_imagenes import (  # noqa: E402
    HashCache,
    ImageFile,
    group_similar_images,
    load_images_from_cache,
    max_hamming_distance,
)
from movil_windows import (  # noqa: E402
    CACHE_MARKER,
    SHELL_OPERATIONS_CSHARP,
    MobileDevice,
    MobileImage,
    _load_manifest,
    _remove_manifest_images,
    _save_manifest,
    _start_powershell_script,
    copy_images_from_device,
)


def brute_force_groups(hashes: list[int], threshold: int) -> set[frozenset[int]]:
    parents = list(range(len(hashes)))

    def find(item: int) -> int:
        while parents[item] != item:
            parents[item] = parents[parents[item]]
            item = parents[item]
        return item

    def union(first: int, second: int) -> None:
        first, second = find(first), find(second)
        if first != second:
            parents[second] = first

    distance = max_hamming_distance(threshold)
    for first in range(len(hashes)):
        for second in range(first + 1, len(hashes)):
            if (hashes[first] ^ hashes[second]).bit_count() <= distance:
                union(first, second)
    grouped: dict[int, set[int]] = {}
    for item in range(len(hashes)):
        grouped.setdefault(find(item), set()).add(item)
    return {frozenset(members) for members in grouped.values() if len(members) > 1}


class SimilarityIndexTests(unittest.TestCase):
    def test_index_matches_exhaustive_comparison_for_every_threshold(self) -> None:
        randomizer = random.Random(20260816)
        hashes = [randomizer.getrandbits(64) for _ in range(110)]
        # Se añaden vecinos conocidos para que todos los umbrales produzcan grupos.
        base = hashes[0]
        hashes.extend((base, base ^ 1, base ^ 0b111111, base ^ ((1 << 13) - 1)))
        for threshold in (80, 90, 95, 100):
            expected = brute_force_groups(hashes, threshold)
            actual = {
                frozenset(group)
                for group in group_similar_images(hashes, threshold, show_progress=False)
            }
            self.assertEqual(expected, actual, f"umbral {threshold}")

    def test_unchanged_unreadable_file_is_not_decoded_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "dañada.jpg"
            path.write_bytes(b"no es una imagen")
            known = [ImageFile(str(path), path.stat().st_size, 0)]
            cache = HashCache(root / "cache.sqlite")
            try:
                with patch(
                    "agrupar_imagenes.perceptual_hash",
                    return_value=(str(path), None, "imagen dañada"),
                ) as decoder:
                    first = load_images_from_cache(root, cache, 1, 90, known_files=known)
                    second = load_images_from_cache(root, cache, 1, 90, known_files=known)
            finally:
                cache.close()
            self.assertEqual(1, decoder.call_count)
            self.assertEqual(first.unreadable, second.unreadable)


class MobileManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.cache = Path(self.temporary.name)
        (self.cache / CACHE_MARKER).write_text("test", encoding="utf-8")
        self.device = MobileDevice("Móvil de prueba", "::device-test")
        self.first_path = self.cache / "Memoria interna" / "DCIM" / "primera.jpg"
        self.second_path = self.cache / "Memoria interna" / "Pictures" / "segunda.jpg"
        self.first_path.parent.mkdir(parents=True)
        self.second_path.parent.mkdir(parents=True)
        self.first_path.write_bytes(b"primera")
        self.second_path.write_bytes(b"segunda")
        self.first = MobileImage(
            str(self.first_path.resolve()), self.device.shell_path,
            r"Memoria interna\DCIM", self.first_path.name, self.first_path.stat().st_size,
        )
        self.second = MobileImage(
            str(self.second_path.resolve()), self.device.shell_path,
            r"Memoria interna\Pictures", self.second_path.name, self.second_path.stat().st_size,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_manifest_round_trip_and_instant_cache_path(self) -> None:
        mapping = {self.first.local_path: self.first, self.second.local_path: self.second}
        _save_manifest(self.cache, self.device, mapping)
        self.assertEqual(mapping, _load_manifest(self.cache, self.device))
        updates = []
        with (
            patch("movil_windows._cache_directory", return_value=self.cache),
            patch("movil_windows._start_powershell_script", side_effect=AssertionError("no debe abrir MTP")),
        ):
            destination, cached = copy_images_from_device(self.device, updates.append)
        self.assertEqual(self.cache, destination)
        self.assertEqual(mapping, cached)
        self.assertEqual("cache", updates[-1].phase)

    def test_confirmed_deletion_is_removed_from_manifest(self) -> None:
        mapping = {self.first.local_path: self.first, self.second.local_path: self.second}
        _save_manifest(self.cache, self.device, mapping)
        with patch("movil_windows._cache_directory", return_value=self.cache):
            _remove_manifest_images({self.first})
        self.assertEqual({self.second.local_path: self.second}, _load_manifest(self.cache, self.device))

    def test_manifest_rejects_changed_local_copy(self) -> None:
        _save_manifest(self.cache, self.device, {self.first.local_path: self.first})
        self.first_path.write_bytes(b"contenido cambiado")
        self.assertEqual({}, _load_manifest(self.cache, self.device))


@unittest.skipUnless(os.name == "nt", "IFileOperation solo existe en Windows")
class NativeBatchDeletionTests(unittest.TestCase):
    def test_native_batch_deletes_generated_files_without_shell_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "primera-prueba.jpg"
            second = root / "segunda-prueba.jpg"
            first.write_bytes(b"prueba")
            second.write_bytes(b"prueba")
            quoted_root = str(root).replace("'", "''")
            script = f"""
            $ErrorActionPreference = 'Stop'
            $source = @'
{SHELL_OPERATIONS_CSHARP}
'@
            Add-Type -TypeDefinition $source
            $shell = New-Object -ComObject Shell.Application
            $folder = $shell.NameSpace('{quoted_root}')
            $items = [object[]]@($folder.ParseName('primera-prueba.jpg'), $folder.ParseName('segunda-prueba.jpg'))
            $errorText = ''
            $success = [MobileShellBatch]::Delete($items, [ref]$errorText)
            [PSCustomObject]@{{ success = $success; error = $errorText }} | ConvertTo-Json -Compress
            """
            process, script_path = _start_powershell_script(script)
            try:
                stdout, stderr = process.communicate(timeout=20)
            finally:
                script_path.unlink(missing_ok=True)
            self.assertEqual(0, process.returncode, stderr)
            self.assertIn('"success":true', stdout.lower())
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())


if __name__ == "__main__":
    unittest.main()
