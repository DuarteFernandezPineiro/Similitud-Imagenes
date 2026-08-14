#!/usr/bin/env python3
"""Interfaz de escritorio para revisar y borrar imágenes similares."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from PIL import Image, ImageOps, ImageTk, UnidentifiedImageError

from agrupar_imagenes import ALLOWED_THRESHOLDS, AnalysisResult, analyze_folder


THUMBNAIL_SIZE = (156, 118)
HOLD_TO_ENLARGE_MS = 500


def load_thumbnail(path_as_text: str) -> Image.Image | None:
    """Abre una miniatura fuera del hilo de la interfaz para mantenerla ágil."""
    try:
        with Image.open(path_as_text) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", THUMBNAIL_SIZE, "#edf1f5")
            offset = ((THUMBNAIL_SIZE[0] - image.width) // 2, (THUMBNAIL_SIZE[1] - image.height) // 2)
            canvas.paste(image, offset)
            return canvas
    except (OSError, UnidentifiedImageError, ValueError):
        return None


class SimilarityApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Similitud de imágenes")
        self.geometry("1240x820")
        self.minsize(850, 620)
        self.configure(background="#f4f6f8")

        self.folder = tk.StringVar()
        self.threshold = tk.IntVar(value=90)
        self.status = tk.StringVar(value="Selecciona una carpeta para comenzar.")
        self.summary = tk.StringVar(value="Aún no hay resultados.")
        self.groups: list[list[str]] = []
        self.selections: dict[int, set[str]] = {}
        self.tiles: dict[tuple[int, str], tuple[tk.Frame, tk.Label]] = {}
        self.group_frames: dict[int, tk.Frame] = {}
        self.group_titles: dict[int, tk.StringVar] = {}
        self.photos: dict[tuple[int, str], ImageTk.PhotoImage] = {}
        self.events: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self.thumbnail_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="miniaturas")
        self.pending_thumbnails: deque[tuple[int, int, str]] = deque()
        self.active_thumbnails = 0
        self.run_id = 0
        self.analysis_running = False
        self.press_timer: str | None = None
        self.long_press_opened = False

        self._configure_styles()
        self._build_interface()
        self.after(40, self._consume_events)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("App.TFrame", background="#f4f6f8")
        style.configure("Card.TFrame", background="white")
        style.configure("Title.TLabel", background="#f4f6f8", font=("Segoe UI", 17, "bold"), foreground="#152536")
        style.configure("Body.TLabel", background="#f4f6f8", foreground="#536273", font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background="white", foreground="#152536", font=("Segoe UI", 12, "bold"))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

    def _build_interface(self) -> None:
        controls = ttk.Frame(self, style="App.TFrame", padding=(22, 18, 22, 10))
        controls.pack(fill="x")
        ttk.Label(controls, text="Imágenes similares", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            controls,
            text="Elige una carpeta, calcula los grupos y selecciona las fotos que quieras eliminar.",
            style="Body.TLabel",
        ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(3, 14))

        ttk.Label(controls, text="Carpeta", style="Body.TLabel").grid(row=2, column=0, sticky="w")
        self.folder_entry = ttk.Entry(controls, textvariable=self.folder, width=76)
        self.folder_entry.grid(row=3, column=0, columnspan=2, sticky="ew", padx=(0, 8), pady=(3, 0))
        ttk.Button(controls, text="Elegir carpeta…", command=self._choose_folder).grid(row=3, column=2, sticky="ew", padx=(0, 16), pady=(3, 0))
        ttk.Label(controls, text="Similitud mínima", style="Body.TLabel").grid(row=2, column=3, sticky="w")
        similarity = ttk.Combobox(controls, state="readonly", width=7, values=ALLOWED_THRESHOLDS, textvariable=self.threshold)
        similarity.grid(row=3, column=3, sticky="w", pady=(3, 0))
        self.analyze_button = ttk.Button(controls, text="Calcular grupos", style="Accent.TButton", command=self._start_analysis)
        self.analyze_button.grid(row=3, column=4, sticky="e", padx=(16, 0), pady=(3, 0))
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)

        feedback = ttk.Frame(self, style="App.TFrame", padding=(22, 0, 22, 11))
        feedback.pack(fill="x")
        self.progress = ttk.Progressbar(feedback, mode="indeterminate", length=160)
        self.progress.pack(side="left", padx=(0, 10))
        ttk.Label(feedback, textvariable=self.status, style="Body.TLabel").pack(side="left")
        ttk.Label(feedback, textvariable=self.summary, style="Body.TLabel").pack(side="right")

        gallery_container = ttk.Frame(self, style="App.TFrame", padding=(22, 0, 10, 18))
        gallery_container.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(gallery_container, background="#f4f6f8", highlightthickness=0)
        scrollbar = ttk.Scrollbar(gallery_container, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.gallery = tk.Frame(self.canvas, background="#f4f6f8")
        self.gallery_window = self.canvas.create_window((0, 0), window=self.gallery, anchor="nw")
        self.gallery.bind("<Configure>", self._update_scroll_region)
        self.canvas.bind("<Configure>", self._fit_gallery_width)
        self.canvas.bind_all("<MouseWheel>", self._scroll_gallery)

        self.empty_label = ttk.Label(
            self.gallery,
            text="Los grupos de imágenes aparecerán aquí.",
            style="Body.TLabel",
            padding=42,
        )
        self.empty_label.pack()

    def _choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="Selecciona la carpeta de fotos", mustexist=True)
        if folder:
            self.folder.set(folder)

    def _start_analysis(self) -> None:
        if self.analysis_running:
            return
        folder = Path(self.folder.get().strip()).expanduser()
        if not folder.is_dir():
            messagebox.showwarning("Carpeta necesaria", "Selecciona una carpeta válida de imágenes.", parent=self)
            return
        self.analysis_running = True
        self.analyze_button.configure(state="disabled")
        self.status.set("Analizando imágenes y actualizando grupos…")
        self.progress.start(12)
        self.run_id += 1
        current_run = self.run_id
        workers = max(1, min(os.cpu_count() or 1, 4))

        def work() -> None:
            try:
                result = analyze_folder(folder, self.threshold.get(), workers)
                self.events.put(("analysis", current_run, result, None))
            except Exception as error:  # Se muestra al usuario sin cerrar la aplicación.
                self.events.put(("analysis", current_run, None, error))

        threading.Thread(target=work, daemon=True, name="analisis-imagenes").start()

    def _consume_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "analysis":
                    self._finish_analysis(*event[1:])
                elif event[0] == "thumbnail":
                    self._show_thumbnail(*event[1:])
        except queue.Empty:
            pass
        self.after(40, self._consume_events)

    def _finish_analysis(self, event_run: int, result: AnalysisResult | None, error: Exception | None) -> None:
        if event_run != self.run_id:
            return
        self.analysis_running = False
        self.analyze_button.configure(state="normal")
        self.progress.stop()
        if error:
            self.status.set("No se pudo completar el análisis.")
            messagebox.showerror("Error al analizar", str(error), parent=self)
            return
        assert result is not None
        self.groups = [list(group) for group in result.groups]
        self.selections.clear()
        self.summary.set(f"{result.image_count:,} imágenes · {len(self.groups):,} grupos")
        mode = "actualización incremental" if result.sync.full_rebuild is False else "grupos reconstruidos"
        self.status.set(
            f"Listo: {result.sync.calculated:,} hashes calculados, {result.sync.reused:,} reutilizados · {mode}."
        )
        self._render_groups()

    def _render_groups(self) -> None:
        self.run_id += 1
        thumbnail_run = self.run_id
        self.pending_thumbnails.clear()
        self.active_thumbnails = 0
        self.photos.clear()
        self.tiles.clear()
        self.group_frames.clear()
        self.group_titles.clear()
        for child in self.gallery.winfo_children():
            child.destroy()

        if not self.groups:
            ttk.Label(
                self.gallery,
                text="No se han encontrado grupos con la similitud seleccionada.",
                style="Body.TLabel",
                padding=42,
            ).pack()
            return

        for group_id, paths in enumerate(self.groups):
            self._create_group_card(group_id, paths, thumbnail_run)
        self._start_thumbnail_jobs(thumbnail_run)
        self.canvas.yview_moveto(0)

    def _create_group_card(self, group_id: int, paths: list[str], thumbnail_run: int) -> None:
        card = tk.Frame(self.gallery, background="white", highlightbackground="#dce3ea", highlightthickness=1)
        card.pack(fill="x", pady=(0, 14), padx=(0, 12))
        self.group_frames[group_id] = card
        title = tk.StringVar()
        self.group_titles[group_id] = title
        self._update_group_title(group_id)
        header = tk.Frame(card, background="white", padx=14, pady=11)
        header.pack(fill="x")
        tk.Label(header, textvariable=title, background="white", foreground="#152536", font=("Segoe UI", 12, "bold")).pack(side="left")
        tk.Button(
            header, text="Seleccionar todas", command=lambda: self._select_all(group_id),
            relief="flat", background="#e9f1fb", foreground="#1d5fa7", padx=9, pady=4,
        ).pack(side="right", padx=(8, 0))
        tk.Button(
            header, text="Eliminar seleccionadas", command=lambda: self._delete_selected(group_id),
            relief="flat", background="#b42318", foreground="white", activebackground="#8f1a12", activeforeground="white", padx=9, pady=4,
        ).pack(side="right")
        tk.Label(
            header, text="Pulsa para seleccionar · mantén pulsado para ampliar",
            background="white", foreground="#6b7785", font=("Segoe UI", 9),
        ).pack(side="right", padx=16)

        grid = tk.Frame(card, background="white", padx=12, pady=14)
        grid.pack(fill="x")
        for index, path in enumerate(paths):
            row, column = divmod(index, 6)
            tile_box = tk.Frame(
                grid, background="#edf1f5", width=166, height=142,
                highlightbackground="#edf1f5", highlightthickness=3,
            )
            tile_box.grid(row=row, column=column, padx=5, pady=5, sticky="n")
            tile_box.grid_propagate(False)
            tile = tk.Label(
                tile_box, text="Cargando…", justify="center", wraplength=150,
                background="#edf1f5", foreground="#52606d", cursor="hand2",
            )
            tile.pack(fill="both", expand=True)
            self.tiles[(group_id, path)] = (tile_box, tile)
            self._bind_tile(tile_box, tile, group_id, path)
            self.pending_thumbnails.append((thumbnail_run, group_id, path))

    def _start_thumbnail_jobs(self, thumbnail_run: int) -> None:
        while self.active_thumbnails < 8 and self.pending_thumbnails:
            queued_run, group_id, path = self.pending_thumbnails.popleft()
            if queued_run != thumbnail_run:
                continue
            self.active_thumbnails += 1
            future = self.thumbnail_pool.submit(load_thumbnail, path)
            future.add_done_callback(
                lambda completed, run=queued_run, group=group_id, image_path=path: self._thumbnail_ready(
                    completed, run, group, image_path
                )
            )

    def _thumbnail_ready(self, future: Future[Image.Image | None], thumbnail_run: int, group_id: int, path: str) -> None:
        try:
            image = future.result()
        except Exception:
            image = None
        self.events.put(("thumbnail", thumbnail_run, group_id, path, image))

    def _show_thumbnail(self, thumbnail_run: int, group_id: int, path: str, image: Image.Image | None) -> None:
        self.active_thumbnails = max(0, self.active_thumbnails - 1)
        if thumbnail_run == self.run_id:
            tile_widgets = self.tiles.get((group_id, path))
            if tile_widgets and tile_widgets[1].winfo_exists():
                _, tile = tile_widgets
                if image is None:
                    tile.configure(text="No se puede\nmostrar", image="")
                else:
                    photo = ImageTk.PhotoImage(image)
                    self.photos[(group_id, path)] = photo
                    tile.configure(image=photo, text="")
                self._paint_selection(group_id, path)
        self._start_thumbnail_jobs(self.run_id)

    def _bind_tile(self, tile_box: tk.Frame, tile: tk.Label, group_id: int, path: str) -> None:
        for widget in (tile_box, tile):
            widget.bind("<ButtonPress-1>", lambda event: self._press_tile(group_id, path))
            widget.bind("<ButtonRelease-1>", lambda event: self._release_tile(group_id, path))
            widget.bind("<Leave>", lambda event: self._cancel_hold())

    def _press_tile(self, group_id: int, path: str) -> None:
        self._cancel_hold()
        self.long_press_opened = False
        self.press_timer = self.after(HOLD_TO_ENLARGE_MS, lambda: self._open_from_hold(path))

    def _release_tile(self, group_id: int, path: str) -> None:
        was_hold = self.long_press_opened
        self._cancel_hold()
        if not was_hold:
            selected = self.selections.setdefault(group_id, set())
            if path in selected:
                selected.remove(path)
            else:
                selected.add(path)
            self._paint_selection(group_id, path)

    def _cancel_hold(self) -> None:
        if self.press_timer is not None:
            try:
                self.after_cancel(self.press_timer)
            except tk.TclError:
                pass
            self.press_timer = None

    def _open_from_hold(self, path: str) -> None:
        self.press_timer = None
        self.long_press_opened = True
        self._show_preview(path)

    def _paint_selection(self, group_id: int, path: str) -> None:
        tile_widgets = self.tiles.get((group_id, path))
        if not tile_widgets or not tile_widgets[0].winfo_exists():
            return
        tile_box, _ = tile_widgets
        selected = path in self.selections.get(group_id, set())
        tile_box.configure(highlightbackground="#1677d2" if selected else "#edf1f5", highlightthickness=3)

    def _select_all(self, group_id: int) -> None:
        paths = self.groups[group_id]
        selected = self.selections.setdefault(group_id, set())
        if len(selected) == len(paths):
            selected.clear()
        else:
            selected.update(paths)
        for path in paths:
            self._paint_selection(group_id, path)

    def _delete_selected(self, group_id: int) -> None:
        selected = set(self.selections.get(group_id, set()))
        if not selected:
            messagebox.showinfo("Sin selección", "Selecciona una o más imágenes de este grupo.", parent=self)
            return
        message = (
            f"Vas a eliminar permanentemente {len(selected)} imagen(es) del disco.\n\n"
            "No se moverán a la papelera y esta acción no se puede deshacer."
        )
        if not messagebox.askyesno("Confirmar eliminación permanente", message, icon="warning", parent=self):
            return

        deleted: set[str] = set()
        failures: list[str] = []
        for path in selected:
            try:
                Path(path).unlink()
                deleted.add(path)
            except OSError as error:
                failures.append(f"{Path(path).name}: {error}")
        if deleted:
            self.groups[group_id] = [path for path in self.groups[group_id] if path not in deleted]
            for path in deleted:
                tile_widgets = self.tiles.pop((group_id, path), None)
                self.photos.pop((group_id, path), None)
                if tile_widgets and tile_widgets[0].winfo_exists():
                    tile_widgets[0].destroy()
            self.selections[group_id].difference_update(deleted)
            if self.groups[group_id]:
                self._update_group_title(group_id)
            else:
                frame = self.group_frames.get(group_id)
                if frame and frame.winfo_exists():
                    frame.destroy()
            self.status.set(
                f"Se eliminaron {len(deleted)} imagen(es). Pulsa «Calcular grupos» para actualizar la caché y los grupos."
            )
        if failures:
            messagebox.showerror("No se pudieron eliminar algunas imágenes", "\n".join(failures[:8]), parent=self)

    def _update_group_title(self, group_id: int) -> None:
        title = self.group_titles.get(group_id)
        if title:
            count = len(self.groups[group_id])
            title.set(f"Grupo {group_id + 1} · {count} imagen{'es' if count != 1 else ''}")

    def _show_preview(self, path: str) -> None:
        try:
            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                max_size = (int(self.winfo_screenwidth() * 0.82), int(self.winfo_screenheight() * 0.78))
                image.thumbnail(max_size, Image.Resampling.LANCZOS)
                preview = image.copy()
        except (OSError, UnidentifiedImageError, ValueError) as error:
            messagebox.showerror("No se puede abrir la imagen", str(error), parent=self)
            return

        window = tk.Toplevel(self)
        window.title(Path(path).name)
        window.configure(background="#111820")
        window.transient(self)
        photo = ImageTk.PhotoImage(preview)
        image_label = tk.Label(window, image=photo, background="#111820")
        image_label.image = photo
        image_label.pack(padx=16, pady=(16, 8))
        tk.Label(window, text=path, background="#111820", foreground="#d7dee8", wraplength=900).pack(padx=16, pady=(0, 10))
        tk.Button(window, text="Cerrar", command=window.destroy, relief="flat", padx=12, pady=5).pack(pady=(0, 14))
        window.bind("<Escape>", lambda event: window.destroy())
        window.grab_set()

    def _update_scroll_region(self, event: tk.Event[Any]) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _fit_gallery_width(self, event: tk.Event[Any]) -> None:
        self.canvas.itemconfigure(self.gallery_window, width=event.width)

    def _scroll_gallery(self, event: tk.Event[Any]) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _close(self) -> None:
        self.thumbnail_pool.shutdown(wait=False, cancel_futures=True)
        self.destroy()


if __name__ == "__main__":
    SimilarityApp().mainloop()
