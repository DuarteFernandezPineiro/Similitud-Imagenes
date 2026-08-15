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


HOLD_TO_ENLARGE_MS = 500


def load_thumbnail(path_as_text: str, maximum_size: tuple[int, int]) -> Image.Image | None:
    """Abre una miniatura fuera del hilo de la interfaz para mantenerla ágil."""
    try:
        with Image.open(path_as_text) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail(maximum_size, Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", maximum_size, "#151b23")
            offset = ((maximum_size[0] - image.width) // 2, (maximum_size[1] - image.height) // 2)
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
        self.after(0, lambda: self.state("zoomed"))

        self.folder = tk.StringVar()
        self.threshold = tk.IntVar(value=90)
        self.images_per_row = tk.IntVar(value=3)
        self.status = tk.StringVar(value="Selecciona una carpeta para comenzar.")
        self.summary = tk.StringVar(value="Aún no hay resultados.")
        self.current_group_text = tk.StringVar(value="Aún no hay ningún grupo para revisar.")
        self.groups: list[list[str]] = []
        self.current_group = 0
        self.image_count = 0
        self.view_mode = "group"
        self.overview_scroll_position = 0.0
        self.selections: dict[int, set[str]] = {}
        self.tiles: dict[tuple[int, str], tuple[tk.Frame, tk.Label]] = {}
        self.photos: dict[tuple[int, str], ImageTk.PhotoImage] = {}
        self.events: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self.thumbnail_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="miniaturas")
        self.pending_thumbnails: deque[tuple[int, int, str, tuple[int, int]]] = deque()
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
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(3, 14))

        ttk.Label(controls, text="Carpeta", style="Body.TLabel").grid(row=2, column=0, sticky="w")
        self.folder_entry = ttk.Entry(controls, textvariable=self.folder, width=76)
        self.folder_entry.grid(row=3, column=0, columnspan=2, sticky="ew", padx=(0, 8), pady=(3, 0))
        ttk.Button(controls, text="Elegir carpeta…", command=self._choose_folder).grid(row=3, column=2, sticky="ew", padx=(0, 16), pady=(3, 0))
        ttk.Label(controls, text="Similitud mínima", style="Body.TLabel").grid(row=2, column=3, sticky="w")
        similarity = ttk.Combobox(controls, state="readonly", width=7, values=ALLOWED_THRESHOLDS, textvariable=self.threshold)
        similarity.grid(row=3, column=3, sticky="w", pady=(3, 0))
        ttk.Label(controls, text="Imágenes por fila", style="Body.TLabel").grid(row=2, column=4, sticky="w", padx=(16, 0))
        columns = ttk.Combobox(controls, state="readonly", width=4, values=(1, 2, 3, 4, 5, 6), textvariable=self.images_per_row)
        columns.grid(row=3, column=4, sticky="w", padx=(16, 0), pady=(3, 0))
        columns.bind("<<ComboboxSelected>>", self._change_columns)
        self.analyze_button = ttk.Button(controls, text="Calcular grupos", style="Accent.TButton", command=self._start_analysis)
        self.analyze_button.grid(row=3, column=5, sticky="e", padx=(16, 0), pady=(3, 0))
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)

        feedback = ttk.Frame(self, style="App.TFrame", padding=(22, 0, 22, 11))
        feedback.pack(fill="x")
        self.progress = ttk.Progressbar(feedback, mode="indeterminate", length=160)
        self.progress.pack(side="left", padx=(0, 10))
        ttk.Label(feedback, textvariable=self.status, style="Body.TLabel").pack(side="left")
        ttk.Label(feedback, textvariable=self.summary, style="Body.TLabel").pack(side="right")

        navigation = tk.Frame(self, background="white", highlightbackground="#dce3ea", highlightthickness=1, padx=18, pady=10)
        navigation.pack(fill="x", padx=22, pady=(0, 10))
        tk.Label(navigation, textvariable=self.current_group_text, background="white", foreground="#152536", font=("Segoe UI", 13, "bold")).pack(side="left")
        self.next_button = tk.Button(
            navigation, text="Siguiente grupo ›", command=lambda: self._go_to_group(1),
            relief="flat", background="#e9f1fb", foreground="#1d5fa7", padx=12, pady=6,
        )
        self.next_button.pack(side="right", padx=(8, 0))
        self.previous_button = tk.Button(
            navigation, text="‹ Grupo anterior", command=lambda: self._go_to_group(-1),
            relief="flat", background="#e9f1fb", foreground="#1d5fa7", padx=12, pady=6,
        )
        self.previous_button.pack(side="right", padx=(8, 0))
        self.select_all_button = tk.Button(
            navigation, text="Seleccionar todas", command=self._select_current_group,
            relief="flat", background="#e9f1fb", foreground="#1d5fa7", padx=10, pady=6,
        )
        self.select_all_button.pack(side="right", padx=(8, 0))
        self.groups_button = tk.Button(
            navigation, text="Ver grupos", command=self._toggle_groups_view,
            relief="flat", background="#26384a", foreground="white", activebackground="#172635", activeforeground="white", padx=10, pady=6,
        )
        self.groups_button.pack(side="right", padx=(8, 0))
        self.delete_button = tk.Button(
            navigation, text="Eliminar seleccionadas", command=self._delete_current_selection,
            relief="flat", background="#b42318", foreground="white", activebackground="#8f1a12", activeforeground="white", padx=10, pady=6,
        )
        self.delete_button.pack(side="right")
        tk.Label(
            navigation, text="Pulsa para seleccionar · mantén pulsado para ampliar",
            background="white", foreground="#6b7785", font=("Segoe UI", 9),
        ).pack(side="right", padx=16)

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
        self._set_navigation_state()

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
        self.current_group = 0
        self.view_mode = "group"
        self.overview_scroll_position = 0.0
        self.image_count = result.image_count
        self.selections.clear()
        self.summary.set(f"{result.image_count:,} imágenes · {len(self.groups):,} grupos")
        mode = "actualización incremental" if result.sync.full_rebuild is False else "grupos reconstruidos"
        self.status.set(
            f"Listo: {result.sync.calculated:,} hashes calculados, {result.sync.reused:,} reutilizados · {mode}."
        )
        self._render_groups()

    def _render_groups(self) -> None:
        self.current_group = 0
        self.view_mode = "group"
        self._render_current_group()

    def _render_current_group(self) -> None:
        self.run_id += 1
        thumbnail_run = self.run_id
        self.pending_thumbnails.clear()
        self.active_thumbnails = 0
        self.photos.clear()
        self.tiles.clear()
        for child in self.gallery.winfo_children():
            child.destroy()

        if not self.groups:
            self.current_group_text.set("No quedan grupos para revisar.")
            self._set_navigation_state()
            ttk.Label(
                self.gallery,
                text="No se han encontrado grupos con la similitud seleccionada.",
                style="Body.TLabel",
                padding=42,
            ).pack()
            return

        self.current_group = max(0, min(self.current_group, len(self.groups) - 1))
        group_id = self.current_group
        paths = self.groups[group_id]
        self.current_group_text.set(
            f"Grupo {group_id + 1} de {len(self.groups)} · {len(paths)} imagen{'es' if len(paths) != 1 else ''}"
        )
        self._set_navigation_state()
        self._create_group_gallery(group_id, paths, thumbnail_run)
        self._start_thumbnail_jobs(thumbnail_run)
        self.canvas.yview_moveto(0)

    def _create_group_gallery(self, group_id: int, paths: list[str], thumbnail_run: int) -> None:
        available_width = max(700, self.canvas.winfo_width() or self.winfo_width() - 56)
        columns = self.images_per_row.get()
        tile_width = max(120, (available_width - 38 - (columns - 1) * 10) // columns)
        tile_height = max(160, min(620, int(tile_width * 0.82)))
        image_bounds = (max(100, tile_width - 10), max(140, tile_height - 10))

        grid = tk.Frame(self.gallery, background="#f4f6f8", padx=8, pady=8)
        grid.pack(fill="x", padx=(0, 12))
        for index, path in enumerate(paths):
            row, column = divmod(index, columns)
            tile_box = tk.Frame(
                grid, background="#151b23", width=tile_width, height=tile_height,
                highlightbackground="#edf1f5", highlightthickness=3,
            )
            tile_box.grid(row=row, column=column, padx=5, pady=5, sticky="n")
            tile_box.grid_propagate(False)
            tile_box.pack_propagate(False)
            tile = tk.Label(
                tile_box, text="Cargando…", justify="center", wraplength=150,
                background="#151b23", foreground="#dce3ea", cursor="hand2",
            )
            tile.pack(fill="both", expand=True)
            self.tiles[(group_id, path)] = (tile_box, tile)
            self._bind_review_tile(tile_box, tile, group_id, path)
            self.pending_thumbnails.append((thumbnail_run, group_id, path, image_bounds))

    def _render_overview(self) -> None:
        self.run_id += 1
        thumbnail_run = self.run_id
        self.pending_thumbnails.clear()
        self.active_thumbnails = 0
        self.photos.clear()
        self.tiles.clear()
        for child in self.gallery.winfo_children():
            child.destroy()

        self.current_group_text.set(f"Vista de grupos · {len(self.groups)} grupos")
        self._set_navigation_state()
        for group_id, paths in enumerate(self.groups):
            card = tk.Frame(self.gallery, background="white", highlightbackground="#dce3ea", highlightthickness=1, cursor="hand2")
            card.pack(fill="x", pady=(0, 14), padx=(0, 12))
            header = tk.Label(
                card, text=f"Grupo {group_id + 1} · {len(paths)} imagen{'es' if len(paths) != 1 else ''}",
                background="white", foreground="#152536", font=("Segoe UI", 12, "bold"), anchor="w", padx=14, pady=11, cursor="hand2",
            )
            header.pack(fill="x")
            grid = tk.Frame(card, background="white", padx=12, pady=12, cursor="hand2")
            grid.pack(fill="x")
            for widget in (card, header, grid):
                widget.bind("<ButtonRelease-1>", lambda event, selected_group=group_id: self._open_group(selected_group))
            for index, path in enumerate(paths):
                row, column = divmod(index, 6)
                tile_box = tk.Frame(
                    grid, background="#151b23", width=166, height=142,
                    highlightbackground="#edf1f5", highlightthickness=2, cursor="hand2",
                )
                tile_box.grid(row=row, column=column, padx=5, pady=5, sticky="n")
                tile_box.grid_propagate(False)
                tile_box.pack_propagate(False)
                tile = tk.Label(tile_box, text="Cargando…", background="#151b23", foreground="#dce3ea", cursor="hand2")
                tile.pack(fill="both", expand=True)
                for widget in (tile_box, tile):
                    widget.bind("<ButtonRelease-1>", lambda event, selected_group=group_id: self._open_group(selected_group))
                self.tiles[(group_id, path)] = (tile_box, tile)
                self.pending_thumbnails.append((thumbnail_run, group_id, path, (156, 132)))
        self._start_thumbnail_jobs(thumbnail_run)
        self.update_idletasks()
        self.canvas.yview_moveto(self.overview_scroll_position)
        self.after(120, lambda: self._restore_overview_scroll(thumbnail_run))

    def _restore_overview_scroll(self, thumbnail_run: int) -> None:
        if self.view_mode != "overview" or thumbnail_run != self.run_id:
            return
        self.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(self.overview_scroll_position)

    def _set_navigation_state(self) -> None:
        has_groups = bool(self.groups)
        reviewing_group = has_groups and self.view_mode == "group"
        self.previous_button.configure(state="normal" if reviewing_group and self.current_group > 0 else "disabled")
        self.next_button.configure(state="normal" if reviewing_group and self.current_group < len(self.groups) - 1 else "disabled")
        action_state = "normal" if reviewing_group else "disabled"
        self.select_all_button.configure(state=action_state)
        self.delete_button.configure(state=action_state)
        self.groups_button.configure(state="normal" if has_groups else "disabled")
        self.groups_button.configure(text="Ver grupos" if self.view_mode == "group" else "Volver al grupo")

    def _go_to_group(self, direction: int) -> None:
        destination = self.current_group + direction
        if 0 <= destination < len(self.groups):
            self._clear_selections()
            self.current_group = destination
            self._render_current_group()

    def _open_group(self, group_id: int) -> None:
        if self.view_mode == "overview":
            self.overview_scroll_position = self.canvas.yview()[0]
        if group_id != self.current_group or self.view_mode == "overview":
            self._clear_selections()
        self.current_group = group_id
        self.view_mode = "group"
        self._render_current_group()

    def _clear_selections(self) -> None:
        self.selections.clear()

    def _toggle_groups_view(self) -> None:
        if not self.groups:
            return
        if self.view_mode == "group":
            self.view_mode = "overview"
            self._render_overview()
        else:
            self.view_mode = "group"
            self._render_current_group()

    def _change_columns(self, event: tk.Event[Any] | None = None) -> None:
        if self.groups:
            if self.view_mode == "group":
                self._render_current_group()

    def _start_thumbnail_jobs(self, thumbnail_run: int) -> None:
        while self.active_thumbnails < 8 and self.pending_thumbnails:
            queued_run, group_id, path, image_bounds = self.pending_thumbnails.popleft()
            if queued_run != thumbnail_run:
                continue
            self.active_thumbnails += 1
            future = self.thumbnail_pool.submit(load_thumbnail, path, image_bounds)
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

    def _bind_review_tile(self, tile_box: tk.Frame, tile: tk.Label, group_id: int, path: str) -> None:
        for widget in (tile_box, tile):
            widget.bind("<ButtonPress-1>", lambda event: self._press_tile(group_id, path))
            widget.bind("<ButtonRelease-1>", lambda event: self._release_tile(group_id, path))
            widget.bind("<Leave>", lambda event: self._cancel_hold())

    def _press_tile(self, group_id: int, path: str) -> None:
        self._cancel_hold()
        self.long_press_opened = False
        self.press_timer = self.after(HOLD_TO_ENLARGE_MS, lambda: self._open_from_hold(group_id, path))

    def _release_tile(self, group_id: int, path: str) -> None:
        was_long_press = self.long_press_opened
        self._cancel_hold()
        if was_long_press:
            return
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

    def _open_from_hold(self, group_id: int, path: str) -> None:
        self.press_timer = None
        self.long_press_opened = True
        self._show_fullscreen_preview(group_id, path)

    def _paint_selection(self, group_id: int, path: str) -> None:
        tile_widgets = self.tiles.get((group_id, path))
        if not tile_widgets or not tile_widgets[0].winfo_exists():
            return
        tile_box, _ = tile_widgets
        selected = path in self.selections.get(group_id, set())
        tile_box.configure(highlightbackground="#1677d2" if selected else "#edf1f5", highlightthickness=3)

    def _select_current_group(self) -> None:
        if not self.groups:
            return
        group_id = self.current_group
        paths = self.groups[group_id]
        selected = self.selections.setdefault(group_id, set())
        if len(selected) == len(paths):
            selected.clear()
        else:
            selected.update(paths)
        for path in paths:
            self._paint_selection(group_id, path)

    def _delete_current_selection(self) -> None:
        if not self.groups:
            return
        group_id = self.current_group
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
            self.image_count -= len(deleted)
            if self.groups[group_id]:
                self.selections[group_id].difference_update(deleted)
            else:
                self.groups.pop(group_id)
                self.selections = {
                    index if index < group_id else index - 1: paths
                    for index, paths in self.selections.items()
                    if index != group_id
                }
                self.current_group = min(group_id, max(0, len(self.groups) - 1))
            self.summary.set(f"{self.image_count:,} imágenes · {len(self.groups):,} grupos")
            self.status.set(
                f"Se eliminaron {len(deleted)} imagen(es). Pulsa «Calcular grupos» para actualizar la caché y los grupos."
            )
            self._render_current_group()
        if failures:
            messagebox.showerror("No se pudieron eliminar algunas imágenes", "\n".join(failures[:8]), parent=self)

    def _show_fullscreen_preview(self, group_id: int, path: str) -> None:
        try:
            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                max_size = (self.winfo_screenwidth() - 80, self.winfo_screenheight() - 150)
                image.thumbnail(max_size, Image.Resampling.LANCZOS)
                preview = image.copy()
        except (OSError, UnidentifiedImageError, ValueError) as error:
            messagebox.showerror("No se puede abrir la imagen", str(error), parent=self)
            return

        window = tk.Toplevel(self)
        window.title(Path(path).name)
        window.configure(background="#111820")
        window.transient(self)
        window.attributes("-fullscreen", True)
        toolbar = tk.Frame(window, background="#111820", padx=22, pady=12)
        toolbar.pack(fill="x")
        tk.Label(toolbar, text=Path(path).name, background="#111820", foreground="#d7dee8", font=("Segoe UI", 11)).pack(side="left")

        selection_button = tk.Button(
            toolbar, relief="flat", padx=12, pady=6, cursor="hand2",
        )
        selection_button.pack(side="right", padx=(10, 0))
        tk.Button(
            toolbar, text="Cerrar · Esc", command=window.destroy, relief="flat",
            background="#e9f1fb", foreground="#1d5fa7", padx=12, pady=6, cursor="hand2",
        ).pack(side="right")

        def update_selection() -> None:
            selected = path in self.selections.get(group_id, set())
            selection_button.configure(
                text="Quitar de la selección" if selected else "Seleccionar para borrar",
                background="#7f1d1d" if selected else "#b42318",
                foreground="white", activebackground="#6b1515", activeforeground="white",
            )

        def toggle_selection() -> None:
            selected = self.selections.setdefault(group_id, set())
            if path in selected:
                selected.remove(path)
            else:
                selected.add(path)
            self._paint_selection(group_id, path)
            update_selection()

        selection_button.configure(command=toggle_selection)
        update_selection()
        photo = ImageTk.PhotoImage(preview)
        image_label = tk.Label(window, image=photo, background="#111820")
        image_label.image = photo
        image_label.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        def close_preview(event: tk.Event[Any] | None = None) -> str | None:
            # bind_all cubre el breve intervalo en que Windows todavía mantiene
            # el foco en la ventana principal al crear el modo pantalla completa.
            self.unbind_all("<Escape>")
            if window.winfo_exists():
                window.destroy()
            return "break" if event else None

        window.bind("<Escape>", close_preview)
        window.bind("<KeyPress-Escape>", close_preview)
        self.bind_all("<Escape>", close_preview, add="+")
        window.protocol("WM_DELETE_WINDOW", close_preview)
        window.grab_set()
        window.lift()
        window.update_idletasks()
        window.focus_force()
        window.after(50, lambda: window.focus_force() if window.winfo_exists() else None)

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
