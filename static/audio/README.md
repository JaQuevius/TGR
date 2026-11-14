# Soundbite Audio Clips Directory

# Soundbite Audio Clips Directory

Create one subfolder per playlist and put its audio files inside that folder.
For example:

```text
static/audio/
  playlist-name/
    song-one.mp3
    song-two.wav
```

## Audio Guidelines
1. **Format**: WAV or MP3, preferably 44.1 kHz, normalized volume.
2. **Duration**: 8 to 12 seconds per clip.
3. **Naming**: The filename stem (without the extension) is shown as the track title.
4. **Licensing**: Only use royalty-free music, Creative Commons Zero (CC0) audio, or original studio recordings. Never use copyrighted commercial tracks without proper rights.

Each immediate subfolder becomes a playlist. MP3 and WAV files inside it become tracks with numeric IDs assigned at startup. The server streams tracks by ID via `/audio/<id>` and supports HTTP 206 Partial Content requests.
