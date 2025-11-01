#  Video Trimmer v4.4
#  - Add: Export still frames from trimmed range (no external dependencies)
#  - Keep: FFmpeg auto-check, modal progress, GPU encode toggle
#  - Clean shutdown and threading-safe progress handling

import os, math, subprocess, cv2, tkinter as tk
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageTk
import shutil, webbrowser, sys
from threading import Thread

# optional drag-drop
DND_AVAILABLE = True
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
except Exception:
    DND_AVAILABLE = False

APP_TITLE = "Video Trimmer (v4.4)"
PREVIEW_MAX_W, PREVIEW_MAX_H = 640, 360


def hhmmss(sec: float) -> str:
    if sec < 0 or math.isnan(sec) or math.isinf(sec):
        return "0:00.00"
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:d}:{m:02d}:{s:05.2f}" if h else f"{m:d}:{s:05.2f}"


def find_ffmpeg_global():
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if shutil.which(exe):
        return shutil.which(exe)
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), exe)
    if os.path.exists(local):
        return local
    return None


def ensure_ffmpeg():
    ffmpeg = find_ffmpeg_global()
    if ffmpeg:
        return ffmpeg
    if messagebox.askyesno(
        "FFmpeg Not Found",
        "This tool requires FFmpeg to process video files.\n\n"
        "Would you like to open the FFmpeg download page?"
    ):
        webbrowser.open("https://ffmpeg.org/download.html")
    sys.exit(0)


# ---------------- Slider ----------------
class TrimSlider(tk.Canvas):
    def __init__(self, master, total_frames=100, command=None, **kw):
        super().__init__(master, height=40, bg="#2b2b2b", highlightthickness=0, **kw)
        self.total, self.command = max(1, total_frames), command
        self.current, self.start, self.end = 0, 0, total_frames
        self.pad, self.r = 12, 6
        self.bind("<Button-1>", self.click)
        self.bind("<B1-Motion>", self.drag)
        self.bind("<Configure>", lambda e: self.redraw())

    def click(self, e): self.move_to_x(e.x)
    def drag(self, e): self.move_to_x(e.x)

    def move_to_x(self, x):
        w = self.winfo_width() - self.pad * 2
        ratio = (x - self.pad) / max(1, w)
        ratio = max(0, min(1, ratio))
        self.current = int(ratio * (self.total - 1))
        self.redraw()
        if self.command:
            self.command(self.current)

    def set_total(self, total):
        self.total = max(1, total)
        self.redraw()

    def set_positions(self, cur=None, start=None, end=None):
        if cur is not None:   self.current = int(max(0, min(cur, self.total - 1)))
        if start is not None: self.start = int(max(0, min(start, self.total - 1)))
        if end is not None:   self.end = int(max(self.start + 1, min(end, self.total)))
        self.redraw()

    def redraw(self):
        self.delete("all")
        w = self.winfo_width() - self.pad * 2
        y = 20
        self.create_line(self.pad, y, self.pad + w, y, fill="#555", width=4, capstyle="round")
        if self.end > self.start:
            x0 = self.pad + (self.start / max(1, self.total - 1)) * w
            x1 = self.pad + (self.end / max(1, self.total - 1)) * w
            self.create_line(x0, y, x1, y, fill="#2196F3", width=6, capstyle="round")
        hx = self.pad + (self.current / max(1, self.total - 1)) * w
        self.create_oval(hx - self.r, y - self.r, hx + self.r, y + self.r,
                         fill="#fff", outline="#000", width=1)


# ---------------- Main App ----------------
class VideoTrimmerApp:
    def __init__(self, root, ffmpeg_path: str):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("900x760")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.cap = None
        self.video_path = None
        self.total_frames = 0
        self.fps = 30.0
        self.current_frame = 0
        self.fit_to_window = tk.BooleanVar(value=True)
        self.use_gpu = tk.BooleanVar(value=False)
        self.export_stills = tk.BooleanVar(value=False)
        self.ffmpeg_path = ffmpeg_path

        # --- Top bar
        top = tk.Frame(root)
        top.pack(fill="x", padx=10, pady=(10, 4))
        tk.Button(top, text="Load Video", command=self.load_video_dialog).pack(side="left")
        self.path_label = tk.Label(top, text="Drop a video or click Load Video …", anchor="w")
        self.path_label.pack(side="left", padx=10, fill="x", expand=True)
        tk.Checkbutton(top, text="Fit Preview", variable=self.fit_to_window,
                       command=self.refresh_preview).pack(side="right")

        # --- Preview
        pf = tk.Frame(root, bd=1, relief="sunken")
        pf.pack(fill="both", expand=True, padx=10, pady=6)
        self.preview = tk.Label(pf, bg="black")
        self.preview.pack(fill="both", expand=True)

        # --- Drag & drop
        if DND_AVAILABLE:
            root.drop_target_register(DND_FILES)
            root.dnd_bind("<<Drop>>", self.on_drop)
        else:
            self.path_label.config(text="tkinterdnd2 not installed – use Load Video. pip install tkinterdnd2")

        # --- Slider
        self.slider = TrimSlider(root, command=self.on_seek)
        self.slider.pack(fill="x", padx=12, pady=(4, 2))

        # --- Info
        info = tk.Frame(root)
        info.pack(fill="x", padx=14, pady=(0, 8))
        self.frame_label = tk.Label(info, text="Frame: – / –")
        self.frame_label.pack(side="left")
        self.time_label = tk.Label(info, text="Time: – / –")
        self.time_label.pack(side="right")

        # --- Controls
        ctrl = tk.Frame(root)
        ctrl.pack(pady=6)
        tk.Label(ctrl, text="Start").grid(row=0, column=0, sticky="e", padx=4)
        self.start_entry = tk.Entry(ctrl, width=8)
        self.start_entry.grid(row=0, column=1)
        tk.Button(ctrl, text="⟵ Set Start", command=self.set_start).grid(row=0, column=2, padx=6)

        tk.Label(ctrl, text="End").grid(row=1, column=0, sticky="e", padx=4)
        self.end_entry = tk.Entry(ctrl, width=8)
        self.end_entry.grid(row=1, column=1)
        tk.Button(ctrl, text="⟵ Set End", command=self.set_end).grid(row=1, column=2, padx=6)

        self.trim_btn = tk.Button(ctrl, text="Trim && Save", command=self.trim_video, state="disabled")
        self.trim_btn.grid(row=0, column=3, rowspan=2, padx=12)

        # --- Options
        opts = tk.Frame(root)
        opts.pack(pady=(4, 10))
        tk.Checkbutton(opts, text="Use GPU Encoder (NVENC)", variable=self.use_gpu).pack(anchor="w", padx=20)
        tk.Checkbutton(opts, text="Export still frames from trim range", variable=self.export_stills).pack(anchor="w", padx=20)

        self.root.bind("<Configure>", lambda e: self.refresh_preview())

    # ---------- Video Handling ----------
    def load_video_dialog(self):
        p = filedialog.askopenfilename(filetypes=[("Video", "*.mp4 *.mov *.avi *.mkv")])
        if p:
            self.load_video(p)

    def on_drop(self, event):
        raw = event.data.strip()
        if raw.startswith("{") and raw.endswith("}"):
            path = raw[1:-1]
        else:
            parts = raw.split()
            path = " ".join(parts)
        if os.name == "nt":
            path = path.replace("/", "\\")
        path = path.strip('"').strip()
        if not os.path.exists(path):
            messagebox.showerror("Error", f"Cannot open file:\n{path}")
            return
        self.load_video(path)

    def load_video(self, path):
        if self.cap:
            self.cap.release()
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            messagebox.showerror("Error", f"Cannot open {path}")
            return
        self.video_path = path
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        self.fps = float(self.cap.get(cv2.CAP_PROP_FPS)) or 30.0
        self.slider.set_total(self.total_frames)
        self.slider.set_positions(0, 0, min(85, self.total_frames - 1))
        self.start_entry.delete(0, "end"); self.start_entry.insert(0, "0")
        self.end_entry.delete(0, "end");   self.end_entry.insert(0, str(min(85, self.total_frames - 1)))
        self.trim_btn.config(state="normal")
        self.path_label.config(text=self.video_path)
        self.show_frame(0)
        self.update_readout(0)
        self.root.title(f"{APP_TITLE} — {os.path.basename(path)}")

    def grab_frame(self, i):
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, f = self.cap.read()
        return f if ok else None

    def show_frame(self, i):
        if not self.cap:
            return
        f = self.grab_frame(i)
        if f is None:
            return
        rgb = cv2.cvtColor(f, cv2.COLOR_BGR2RGB)
        if self.fit_to_window.get():
            h, w, _ = rgb.shape
            s = min(PREVIEW_MAX_W / w, PREVIEW_MAX_H / h, 1.0)
            rgb = cv2.resize(rgb, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
        imgtk = ImageTk.PhotoImage(Image.fromarray(rgb))
        self.preview.configure(image=imgtk)
        self.preview.image = imgtk

    def update_readout(self, i):
        t = max(0, self.total_frames - 1)
        self.frame_label.config(text=f"Frame: {i} / {t}")
        cur = i / self.fps
        tot = t / self.fps
        self.time_label.config(text=f"Time: {hhmmss(cur)} / {hhmmss(tot)}")

    def refresh_preview(self):
        if self.video_path:
            self.show_frame(self.current_frame)

    def on_seek(self, frame):
        self.current_frame = frame
        self.show_frame(frame)
        self.update_readout(frame)

    def set_start(self):
        self.start_entry.delete(0, "end")
        self.start_entry.insert(0, str(self.current_frame))
        self.slider.set_positions(start=self.current_frame)

    def set_end(self):
        self.end_entry.delete(0, "end")
        self.end_entry.insert(0, str(self.current_frame))
        self.slider.set_positions(end=self.current_frame)

    # ---------- Progress Window ----------
    def show_progress(self, title="Processing…"):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=title).pack(padx=16, pady=(16, 6))
        p = ttk.Progressbar(win, mode="indeterminate", length=320)
        p.pack(padx=16, pady=(0, 12))
        p.start(12)
        ttk.Button(win, text="Hide", command=win.withdraw).pack(pady=(0, 12))
        return win, p

    # ---------- Trimming ----------
    def trim_video(self):
        if not self.video_path:
            return
        try:
            s = int(self.start_entry.get())
            e = int(self.end_entry.get())
        except ValueError:
            messagebox.showerror("Error", "Start/End must be integers.")
            return
        if e <= s:
            messagebox.showerror("Error", "End must be greater than Start.")
            return

        base = os.path.splitext(os.path.basename(self.video_path))[0]
        out = os.path.join(os.path.dirname(self.video_path), f"{base}_trim_{s}_{e}.mp4")
        codec = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "19"] if self.use_gpu.get() \
                else ["-c:v", "libx264", "-crf", "18", "-preset", "fast"]

        cmd = [
            self.ffmpeg_path, "-y", "-i", self.video_path,
            "-vf", f"select='between(n,{s},{e})',setpts=N/FRAME_RATE/TB",
            "-vsync", "0", "-an", *codec, out
        ]

        prog, bar = self.show_progress("Trimming…")

        def work(trim_start, trim_end):
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.root.after(0, lambda: messagebox.showinfo("Saved", f"Trimmed video → {out}"))
                if self.export_stills.get():
                    # ✅ use trim_start and trim_end, not s/e from outer scope
                    self.root.after(0, lambda: self.export_still_frames(trim_start, trim_end))
            except subprocess.CalledProcessError as err:
                self.root.after(0, lambda: self.show_error_log(err))
            except FileNotFoundError:
                self.root.after(0, lambda: messagebox.showerror("Error", "ffmpeg not found."))
            finally:
                self.root.after(0, prog.destroy)

        # ✅ pass start/end explicitly into the thread
        Thread(target=work, args=(s, e), daemon=True).start()



    def export_still_frames(self, s, e):
        if not self.video_path:
            return

        # Build output directory
        out_dir = os.path.join(os.path.dirname(self.video_path), f"frames_trim_{s}_{e}")
        os.makedirs(out_dir, exist_ok=True)

        # Define proper absolute paths
        input_path = os.path.normpath(self.video_path)
        output_pattern = os.path.normpath(os.path.join(out_dir, "frame_%04d.png"))

        # Construct FFmpeg command (no backslashes in the filter)
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_path,
            "-vf", f"select='between(n,{s},{e})',setpts=N/FRAME_RATE/TB",
            "-vsync", "0",
            output_pattern
        ]

        # Show the actual command for debugging
        print("Running FFmpeg:", " ".join(cmd))

        prog, bar = self.show_progress("Exporting Still Frames…")

        def work():
            try:
                result = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.root.after(0, lambda: messagebox.showinfo(
                    "Frames Exported",
                    f"Saved to:\n{out_dir}"
                ))
                # Auto-open the folder in Explorer
                try:
                    os.startfile(out_dir)
                except Exception:
                    pass
            except subprocess.CalledProcessError as e:
                # Show the full FFmpeg error output for visibility
                err_output = e.stderr.decode(errors="ignore") if e.stderr else str(e)
                print("FFmpeg error:\n", err_output)
                self.root.after(0, lambda: self.show_error_log(e))
            finally:
                self.root.after(0, prog.destroy)

        Thread(target=work, daemon=True).start()


    def show_error_log(self, e):
        err = e.stderr.decode(errors="ignore") if hasattr(e, "stderr") and e.stderr else str(e)
        lines = [ln for ln in err.splitlines() if "error" in ln.lower() or "fail" in ln.lower()]
        if not lines:
            lines = err.splitlines()[:20]
        win = tk.Toplevel(self.root)
        win.title("FFmpeg Error Log")
        txt = tk.Text(win, width=100, height=25, bg="#111", fg="#ff5555", wrap="word")
        txt.insert("1.0", "\n".join(lines))
        txt.config(state="disabled")
        txt.pack(fill="both", expand=True)
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=6)

    def on_close(self):
        try:
            if self.cap:
                self.cap.release()
        finally:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass
            self.root.destroy()


def main():
    ffmpeg = ensure_ffmpeg()
    root = TkinterDnD.Tk() if DND_AVAILABLE else tk.Tk()
    app = VideoTrimmerApp(root, ffmpeg_path=ffmpeg)
    root.mainloop()


if __name__ == "__main__":
    main()
    def export_still_frames(self, s, e):
        if not self.video_path:
            return
        out_dir = os.path.join(os.path.dirname(self.video_path), f"frames_trim_{s}_{e}")
        os.makedirs(out_dir, exist_ok=True)

        # Normalize and quote paths safely
        input_path = os.path.normpath(self.video_path)
        output_pattern = os.path.normpath(os.path.join(out_dir, "frame_%04d.png"))

        # Improved filter and proper escaping
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_path,
            "-vf", f"select='between(n\\,{s}\\,{e})',setpts=N/FRAME_RATE/TB",
            "-vsync", "0",
            output_pattern
        ]

        # Debug print for verification
        print("Running FFmpeg:", " ".join(cmd))

        prog, bar = self.show_progress("Exporting Still Frames…")

        def work():
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.root.after(0, lambda: messagebox.showinfo("Frames Exported", f"Saved to {out_dir}"))
                # Optional: auto-open the folder in Explorer
                try:
                    os.startfile(out_dir)
                except Exception:
                    pass
            except subprocess.CalledProcessError as e:
                self.root.after(0, lambda: self.show_error_log(e))
            finally:
                self.root.after(0, prog.destroy)

        Thread(target=work, daemon=True).start()
