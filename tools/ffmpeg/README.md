## ffmpeg bundle directory

This project uses `ffmpeg` to mux video with audio.

### How it works

- By default, scripts will look for `ffmpeg` on `PATH`.
- If not found, they will **download a static ffmpeg build** at runtime and place it here:
  - `tools/ffmpeg/windows/ffmpeg.exe`
  - `tools/ffmpeg/linux/ffmpeg`

### Overrides

- Set `FFMPEG_PATH` to a file path (or command on PATH) to force a specific `ffmpeg`.

