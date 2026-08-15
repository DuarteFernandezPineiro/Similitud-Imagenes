#!/usr/bin/env python3
"""Interfaz de escritorio para revisar y borrar imágenes similares."""

from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from PIL import Image, ImageOps, ImageTk, UnidentifiedImageError

from agrupar_imagenes import ALLOWED_THRESHOLDS, AnalysisProgress, AnalysisResult, analyze_folder


HOLD_TO_ENLARGE_MS = 500
APP_BACKGROUND = "#F5F7FB"
CARD_BACKGROUND = "#FFFFFF"
TEXT_PRIMARY = "#172033"
TEXT_SECONDARY = "#61708A"
ACCENT = "#2563EB"
ACCENT_LIGHT = "#E8F0FF"
BORDER = "#DCE3EE"
DANGER = "#C8332D"


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
        self.configure(background=APP_BACKGROUND)
        self.after(0, lambda: self.state("zoomed"))

        self.folder = tk.StringVar()
        self.threshold = tk.IntVar(value=90)
        self.images_per_row = tk.IntVar(value=3)
        self.status = tk.StringVar(value="Selecciona una carpeta para comenzar.")
        self.summary = tk.StringVar(value="Aún no hay resultados.")
        self.current_group_text = tk.StringVar(value="Aún no hay ningún grupo para revisar.")
        self.selection_text = tk.StringVar(value="Selecciona las fotos que quieras eliminar")
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
        self.analysis_run_id = 0
        self.gallery_run_id = 0
        self.analysis_running = False
        self.live_signature: tuple[tuple[str, ...], ...] = ()
        self.pending_live_groups: list[list[str]] | None = None
        self.last_live_render = 0.0
        self.live_update_timer: str | None = None
        self.press_timer: str | None = None
        self.long_press_opened = False

        self._configure_styles()
        self._build_interface()
        self.after(40, self._consume_events)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("App.TFrame", background=APP_BACKGROUND)
        style.configure("Body.TLabel", background=APP_BACKGROUND, foreground=TEXT_SECONDARY, font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background=CARD_BACKGROUND, foreground=TEXT_PRIMARY, font=("Segoe UI", 12, "bold"))
        style.configure("Path.TEntry", fieldbackground=CARD_BACKGROUND, foreground=TEXT_PRIMARY, padding=(10, 7))
        style.map("Path.TEntry", fieldbackground=[("readonly", CARD_BACKGROUND)])
        style.configure("Choice.TCombobox", padding=(7, 5))
        style.configure("Thin.Horizontal.TProgressbar", troughcolor="#E8EDF5", background=ACCENT, bordercolor="#E8EDF5", lightcolor=ACCENT, darkcolor=ACCENT)

    def _build_interface(self) -> None:
        header = tk.Frame(self, background=CARD_BACKGROUND, padx=28, pady=18)
        header.pack(fill="x")
        tk.Label(header, text="Fotos parecidas", background=CARD_BACKGROUND, foreground=TEXT_PRIMARY, font=("Segoe UI", 20, "bold")).pack(anchor="w")
        tk.Label(
            header,
            text="Encuentra duplicados o fotos muy similares y decide con calma cuáles conservar.",
            background=CARD_BACKGROUND,
            foreground=TEXT_SECONDARY,
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(3, 0))

        controls = tk.Frame(self, background=APP_BACKGROUND, padx=28, pady=16)
        controls.pack(fill="x")
        setup_card = tk.Frame(controls, background=CARD_BACKGROUND, highlightbackground=BORDER, highlightthickness=1, padx=16, pady=14)
        setup_card.pack(fill="x")
        tk.Label(setup_card, text="1. Elige la carpeta de fotos", background=CARD_BACKGROUND, foreground=TEXT_PRIMARY, font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(setup_card, text="El análisis empieza automáticamente.", background=CARD_BACKGROUND, foreground=TEXT_SECONDARY, font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w", pady=(2, 10))
        self.folder_entry = ttk.Entry(setup_card, textvariable=self.folder, state="readonly", style="Path.TEntry")
        self.folder_entry.grid(row=2, column=0, sticky="ew", padx=(0, 10))
        tk.Button(
            setup_card, text="Elegir carpeta", command=self._choose_folder, relief="flat", borderwidth=0,
            background=ACCENT, foreground="white", activebackground="#1D4ED8", activeforeground="white",
            disabledforeground="#D7E4FF", cursor="hand2", font=("Segoe UI", 10, "bold"), padx=16, pady=8,
        ).grid(row=2, column=1, sticky="e")

        options = tk.Frame(setup_card, background=CARD_BACKGROUND)
        options.grid(row=2, column=2, sticky="e", padx=(24, 0))
        tk.Label(options, text="Similitud", background=CARD_BACKGROUND, foreground=TEXT_SECONDARY, font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        similarity = ttk.Combobox(options, state="readonly", width=5, values=ALLOWED_THRESHOLDS, textvariable=self.threshold, style="Choice.TCombobox")
        similarity.grid(row=1, column=0, sticky="w", pady=(2, 0))
        tk.Label(options, text="%", background=CARD_BACKGROUND, foreground=TEXT_SECONDARY, font=("Segoe UI", 9)).grid(row=1, column=1, sticky="w", padx=(3, 12))
        tk.Label(options, text="Fotos por fila", background=CARD_BACKGROUND, foreground=TEXT_SECONDARY, font=("Segoe UI", 9)).grid(row=0, column=2, sticky="w")
        columns = ttk.Combobox(options, state="readonly", width=3, values=(1, 2, 3, 4, 5, 6), textvariable=self.images_per_row, style="Choice.TCombobox")
        columns.grid(row=1, column=2, sticky="w", pady=(2, 0))
        columns.bind("<<ComboboxSelected>>", self._change_columns)
        self.analyze_button = tk.Button(
            options, text="Actualizar", command=self._start_analysis, relief="flat", borderwidth=0,
            background=ACCENT_LIGHT, foreground="#1D4ED8", activebackground="#D8E6FF", activeforeground="#1D4ED8",
            disabledforeground="#8BA3C7", cursor="hand2", font=("Segoe UI", 9, "bold"), padx=12, pady=7,
        )
        self.analyze_button.grid(row=1, column=3, sticky="w", padx=(12, 0))
        setup_card.columnconfigure(0, weight=1)

        feedback = tk.Frame(self, background=APP_BACKGROUND, padx=28)
        feedback.pack(fill="x", pady=(0, 12))
        feedback_card = tk.Frame(feedback, background="#EEF4FF", padx=14, pady=11)
        feedback_card.pack(fill="x")
        self.progress = ttk.Progressbar(feedback_card, mode="indeterminate", length=150, style="Thin.Horizontal.TProgressbar")
        self.progress.pack(side="left", padx=(0, 12))
        tk.Label(feedback_card, textvariable=self.status, background="#EEF4FF", foreground="#36557C", font=("Segoe UI", 9)).pack(side="left")
        tk.Label(feedback_card, textvariable=self.summary, background="#EEF4FF", foreground="#36557C", font=("Segoe UI", 9, "bold")).pack(side="right")

        navigation = tk.Frame(self, background=CARD_BACKGROUND, highlightbackground=BORDER, highlightthickness=1, padx=18, pady=13)
        navigation.pack(fill="x", padx=28, pady=(0, 12))
        context = tk.Frame(navigation, background=CARD_BACKGROUND)
        context.pack(side="left", fill="x", expand=True)
        tk.Label(context, textvariable=self.current_group_text, background=CARD_BACKGROUND, foreground=TEXT_PRIMARY, font=("Segoe UI", 13, "bold")).pack(anchor="w")
        tk.Label(context, textvariable=self.selection_text, background=CARD_BACKGROUND, foreground=TEXT_SECONDARY, font=("Segoe UI", 9)).pack(anchor="w", pady=(3, 0))
        actions = tk.Frame(navigation, background=CARD_BACKGROUND)
        actions.pack(side="right")
        self.next_button = tk.Button(
            actions, text="Siguiente  ›", command=lambda: self._go_to_group(1), relief="flat", borderwidth=0,
            background=ACCENT, foreground="white", activebackground="#1D4ED8", activeforeground="white", disabledforeground="#AFC5E9", padx=13, pady=7, cursor="hand2",
        )
        self.next_button.pack(side="right", padx=(8, 0))
        self.previous_button = tk.Button(
            actions, text="‹ Anterior", command=lambda: self._go_to_group(-1), relief="flat", borderwidth=0,
            background=ACCENT_LIGHT, foreground="#1D4ED8", activebackground="#D8E6FF", activeforeground="#1D4ED8", disabledforeground="#8BA3C7", padx=12, pady=7, cursor="hand2",
        )
        self.previous_button.pack(side="right", padx=(8, 0))
        self.select_all_button = tk.Button(
            actions, text="Seleccionar todas", command=self._select_current_group, relief="flat", borderwidth=0,
            background=ACCENT_LIGHT, foreground="#1D4ED8", activebackground="#D8E6FF", activeforeground="#1D4ED8", disabledforeground="#8BA3C7", padx=10, pady=7, cursor="hand2",
        )
        self.select_all_button.pack(side="right", padx=(8, 0))
        self.groups_button = tk.Button(
            actions, text="Ver grupos", command=self._toggle_groups_view, relief="flat", borderwidth=0,
            background="#24324A", foreground="white", activebackground="#172033", activeforeground="white", disabledforeground="#A5AFBE", padx=11, pady=7, cursor="hand2",
        )
        self.groups_button.pack(side="right", padx=(8, 0))
        self.delete_button = tk.Button(
            actions, text="Eliminar", command=self._delete_current_selection, relief="flat", borderwidth=0,
            background=DANGER, foreground="white", activebackground="#A51F1A", activeforeground="white", disabledforeground="#E9B9B6", padx=12, pady=7, cursor="hand2",
        )
        self.delete_button.pack(side="right")

        gallery_container = ttk.Frame(self, style="App.TFrame", padding=(28, 0, 12, 22))
        gallery_container.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(gallery_container, background=APP_BACKGROUND, highlightthickness=0)
        scrollbar = ttk.Scrollbar(gallery_container, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.gallery = tk.Frame(self.canvas, background=APP_BACKGROUND)
        self.gallery_window = self.canvas.create_window((0, 0), window=self.gallery, anchor="nw")
        self.gallery.bind("<Configure>", self._update_scroll_region)
        self.canvas.bind("<Configure>", self._fit_gallery_width)
        self.canvas.bind_all("<MouseWheel>", self._scroll_gallery)

        self.empty_label = tk.Label(
            self.gallery, text="Elige una carpeta para empezar.\n\nAquí aparecerán las fotos parecidas para revisarlas una a una.",
            background=APP_BACKGROUND, foreground=TEXT_SECONDARY, justify="center", font=("Segoe UI", 11), padx=42, pady=54,
        )
        self.empty_label.pack()
        self._set_navigation_state()

    def _choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="Selecciona la carpeta de fotos", mustexist=True)
        if folder:
            self.folder.set(folder)
            self._start_analysis()

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
        self.summary.set("Preparando el análisis…")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        self.analysis_run_id += 1
        current_run = self.analysis_run_id
        self.groups = []
        self.image_count = 0
        self.current_group = 0
        self.view_mode = "overview"
        self.overview_scroll_position = 0.0
        self.selections.clear()
        self.live_signature = ()
        self.pending_live_groups = None
        self.last_live_render = 0.0
        if self.live_update_timer is not None:
            self.after_cancel(self.live_update_timer)
            self.live_update_timer = None
        self._render_current_group()
        workers = max(1, min(os.cpu_count() or 1, 4))

        def work() -> None:
            try:
                result = analyze_folder(
                    folder,
                    self.threshold.get(),
                    workers,
                    lambda progress: self.events.put(("progress", current_run, progress)),
                )
                self.events.put(("analysis", current_run, result, None))
            except Exception as error:  # Se muestra al usuario sin cerrar la aplicación.
                self.events.put(("analysis", current_run, None, error))

        threading.Thread(target=work, daemon=True, name="analisis-imagenes").start()

    def _consume_events(self) -> None:
        latest_progress: tuple[int, AnalysisProgress] | None = None
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "analysis":
                    self._finish_analysis(*event[1:])
                elif event[0] == "progress":
                    latest_progress = (event[1], event[2])
                elif event[0] == "thumbnail":
                    self._show_thumbnail(*event[1:])
        except queue.Empty:
            pass
        if latest_progress is not None:
            self._show_analysis_progress(*latest_progress)
        self.after(40, self._consume_events)

    def _finish_analysis(self, event_run: int, result: AnalysisResult | None, error: Exception | None) -> None:
        if event_run != self.analysis_run_id:
            return
        self.analysis_running = False
        self.analyze_button.configure(state="normal")
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=100)
        if self.live_update_timer is not None:
            self.after_cancel(self.live_update_timer)
            self.live_update_timer = None
        if error:
            self.status.set("No se pudo completar el análisis.")
            messagebox.showerror("Error al analizar", str(error), parent=self)
            return
        assert result is not None
        had_live_groups = bool(self.groups)
        was_reviewing_group = self.view_mode == "group" and had_live_groups
        previous_paths = list(self.groups[self.current_group]) if was_reviewing_group else []
        previous_selection = set(self.selections.get(self.current_group, set())) if was_reviewing_group else set()
        if self.view_mode == "overview":
            self.overview_scroll_position = self.canvas.yview()[0]
        self.groups = [list(group) for group in result.groups]
        self.pending_live_groups = None
        self.live_signature = tuple(tuple(group) for group in self.groups)
        self.image_count = result.image_count
        self.selections.clear()
        self.summary.set(f"{result.image_count:,} imágenes · {len(self.groups):,} grupos")
        mode = "actualización incremental" if result.sync.full_rebuild is False else "grupos reconstruidos"
        self.status.set(
            f"Listo: {result.sync.calculated:,} hashes calculados, {result.sync.reused:,} reutilizados · {mode}."
        )
        if was_reviewing_group:
            self.current_group = self._matching_group_index(previous_paths, self.groups)
            self.view_mode = "group"
            if self.groups:
                retained_selection = previous_selection.intersection(self.groups[self.current_group])
                if retained_selection:
                    self.selections[self.current_group] = retained_selection
            self._render_current_group()
        elif self.view_mode == "overview" and had_live_groups:
            self._render_overview()
        else:
            self._render_groups()

    def _show_analysis_progress(self, event_run: int, progress: AnalysisProgress) -> None:
        if event_run != self.analysis_run_id or not self.analysis_running:
            return
        self.progress.stop()
        self.progress.configure(
            mode="determinate",
            maximum=max(1, progress.total),
            value=min(progress.processed, progress.total),
        )
        self.status.set(f"Procesando imágenes: {progress.processed:,} de {progress.total:,}…")
        self.summary.set(
            f"{progress.processed:,}/{progress.total:,} procesadas · "
            f"{progress.groups_found:,} grupos disponibles"
        )
        live_groups = self._existing_groups(progress.groups)
        signature = tuple(tuple(group) for group in live_groups)
        if signature == self.live_signature:
            return
        self.live_signature = signature
        if self.view_mode == "group" and self.groups:
            # Mientras se revisa, no se redibuja la galería bajo los pies de
            # la persona usuaria. El siguiente grupo aplica esta actualización.
            self.pending_live_groups = live_groups
            self._set_navigation_state()
            return
        self._queue_or_apply_live_groups(live_groups, keep_current=False)

    def _queue_or_apply_live_groups(self, groups: list[list[str]], keep_current: bool) -> None:
        if time.monotonic() - self.last_live_render >= 0.35:
            self._replace_live_groups(groups, keep_current=keep_current)
            return
        self.pending_live_groups = groups
        if self.live_update_timer is None:
            self.live_update_timer = self.after(350, self._flush_live_groups)

    def _flush_live_groups(self) -> None:
        self.live_update_timer = None
        if not self.analysis_running or self.pending_live_groups is None:
            return
        if self.view_mode == "group":
            return
        groups = self.pending_live_groups
        self._replace_live_groups(groups, keep_current=self.view_mode == "group")

    def _existing_groups(self, groups: list[list[str]]) -> list[list[str]]:
        return [
            existing
            for group in groups
            if len(existing := [path for path in group if Path(path).is_file()]) > 1
        ]

    def _replace_live_groups(self, groups: list[list[str]], keep_current: bool = False) -> None:
        previous_paths: list[str] = []
        selected_paths: set[str] = set()
        if keep_current and self.groups:
            previous_paths = self.groups[self.current_group]
            selected_paths = set(self.selections.get(self.current_group, set()))
        self.groups = groups
        self.pending_live_groups = None
        self.selections.clear()
        self.last_live_render = time.monotonic()
        if keep_current:
            self.current_group = self._matching_group_index(previous_paths, self.groups)
            self.view_mode = "group"
            if self.groups:
                retained_selection = selected_paths.intersection(self.groups[self.current_group])
                if retained_selection:
                    self.selections[self.current_group] = retained_selection
            self._render_current_group()
            return
        self.current_group = 0
        self.view_mode = "overview"
        self._render_overview()

    @staticmethod
    def _matching_group_index(previous_paths: list[str], groups: list[list[str]]) -> int:
        previous_set = set(previous_paths)
        return max(
            range(len(groups)),
            key=lambda index: len(previous_set.intersection(groups[index])),
            default=0,
        )

    def _apply_pending_live_groups(self) -> None:
        if self.pending_live_groups is not None:
            self._replace_live_groups(self.pending_live_groups, keep_current=self.view_mode == "group")

    def _render_groups(self) -> None:
        self.current_group = 0
        self.view_mode = "group"
        self._render_current_group()

    def _render_current_group(self) -> None:
        self.gallery_run_id += 1
        thumbnail_run = self.gallery_run_id
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

        grid = tk.Frame(self.gallery, background=APP_BACKGROUND, padx=8, pady=8)
        grid.pack(fill="x", padx=(0, 12))
        for index, path in enumerate(paths):
            row, column = divmod(index, columns)
            tile_box = tk.Frame(
                grid, background="#111827", width=tile_width, height=tile_height,
                highlightbackground=BORDER, highlightthickness=3,
            )
            tile_box.grid(row=row, column=column, padx=5, pady=5, sticky="n")
            tile_box.grid_propagate(False)
            tile_box.pack_propagate(False)
            tile = tk.Label(
                tile_box, text="Cargando…", justify="center", wraplength=150,
                background="#111827", foreground="#D9E2F0", cursor="hand2",
            )
            tile.pack(fill="both", expand=True)
            self.tiles[(group_id, path)] = (tile_box, tile)
            self._bind_review_tile(tile_box, tile, group_id, path)
            self.pending_thumbnails.append((thumbnail_run, group_id, path, image_bounds))

    def _render_overview(self) -> None:
        self.gallery_run_id += 1
        thumbnail_run = self.gallery_run_id
        self.pending_thumbnails.clear()
        self.active_thumbnails = 0
        self.photos.clear()
        self.tiles.clear()
        for child in self.gallery.winfo_children():
            child.destroy()

        self.current_group_text.set(f"Vista de grupos · {len(self.groups)} grupos")
        self._set_navigation_state()
        for group_id, paths in enumerate(self.groups):
            card = tk.Frame(self.gallery, background=CARD_BACKGROUND, highlightbackground=BORDER, highlightthickness=1, cursor="hand2")
            card.pack(fill="x", pady=(0, 14), padx=(0, 12))
            header = tk.Frame(card, background=CARD_BACKGROUND, padx=14, pady=11, cursor="hand2")
            header.pack(fill="x")
            heading = tk.Label(
                header, text=f"Grupo {group_id + 1} · {len(paths)} imagen{'es' if len(paths) != 1 else ''}",
                background=CARD_BACKGROUND, foreground=TEXT_PRIMARY, font=("Segoe UI", 12, "bold"), anchor="w", cursor="hand2",
            )
            heading.pack(side="left")
            open_label = tk.Label(
                header, text="Revisar  →", background=ACCENT_LIGHT, foreground="#1D4ED8",
                font=("Segoe UI", 9, "bold"), padx=9, pady=4, cursor="hand2",
            )
            open_label.pack(side="right")
            grid = tk.Frame(card, background=CARD_BACKGROUND, padx=12, pady=12, cursor="hand2")
            grid.pack(fill="x")
            for widget in (card, header, heading, open_label, grid):
                widget.bind("<ButtonRelease-1>", lambda event, selected_group=group_id: self._open_group(selected_group))
            for index, path in enumerate(paths):
                row, column = divmod(index, 6)
                tile_box = tk.Frame(
                    grid, background="#111827", width=166, height=142,
                    highlightbackground=BORDER, highlightthickness=2, cursor="hand2",
                )
                tile_box.grid(row=row, column=column, padx=5, pady=5, sticky="n")
                tile_box.grid_propagate(False)
                tile_box.pack_propagate(False)
                tile = tk.Label(tile_box, text="Cargando…", background="#111827", foreground="#D9E2F0", cursor="hand2")
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
        if self.view_mode != "overview" or thumbnail_run != self.gallery_run_id:
            return
        self.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(self.overview_scroll_position)

    def _set_navigation_state(self) -> None:
        has_groups = bool(self.groups)
        reviewing_group = has_groups and self.view_mode == "group"
        self.previous_button.configure(state="normal" if reviewing_group and self.current_group > 0 else "disabled")
        can_go_next = reviewing_group and (
            self.current_group < len(self.groups) - 1 or self.pending_live_groups is not None
        )
        self.next_button.configure(state="normal" if can_go_next else "disabled")
        action_state = "normal" if reviewing_group else "disabled"
        self.select_all_button.configure(state=action_state)
        self.delete_button.configure(state=action_state)
        self.groups_button.configure(state="normal" if has_groups else "disabled")
        self.groups_button.configure(text="Ver grupos" if self.view_mode == "group" else "Volver al grupo")
        if reviewing_group:
            selected_count = len(self.selections.get(self.current_group, set()))
            group_size = len(self.groups[self.current_group])
            if selected_count:
                self.selection_text.set(
                    f"{selected_count} seleccionada{'s' if selected_count != 1 else ''} para eliminar"
                )
            else:
                self.selection_text.set("Pulsa una foto para seleccionarla · mantén pulsado para verla grande")
            self.delete_button.configure(text=f"Eliminar ({selected_count})" if selected_count else "Eliminar")
            self.select_all_button.configure(
                text="Quitar selección" if selected_count == group_size else "Seleccionar todas"
            )
        elif has_groups:
            self.selection_text.set("Elige un grupo para revisar sus fotos.")
            self.delete_button.configure(text="Eliminar")
            self.select_all_button.configure(text="Seleccionar todas")
        else:
            self.selection_text.set("Elige una carpeta para empezar.")
            self.delete_button.configure(text="Eliminar")
            self.select_all_button.configure(text="Seleccionar todas")

    def _go_to_group(self, direction: int) -> None:
        if direction > 0 and self.pending_live_groups is not None:
            self._replace_live_groups(self.pending_live_groups, keep_current=True)
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
            if self.pending_live_groups is not None:
                self._replace_live_groups(self.pending_live_groups)
                return
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
        if thumbnail_run == self.gallery_run_id:
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
        self._start_thumbnail_jobs(self.gallery_run_id)

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
        self._set_navigation_state()

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
        tile_box.configure(highlightbackground=ACCENT if selected else BORDER, highlightthickness=4 if selected else 3)

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
        self._set_navigation_state()

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
                f"Se eliminaron {len(deleted)} imagen(es). Pulsa «Actualizar» para actualizar los grupos."
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
            self._set_navigation_state()
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
