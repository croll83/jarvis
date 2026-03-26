# Video Assembly Reference (ffmpeg)

ffmpeg è disponibile su GB10 (`/usr/bin/ffmpeg`).

## Concatenare clip video

### 1. Creare file lista
```bash
cat > concat_list.txt << 'EOF'
file 'clip1.webp'
file 'clip2.webp'
file 'clip3.webp'
EOF
```

### 2. Concatenare
```bash
ffmpeg -f concat -safe 0 -i concat_list.txt -c:v libx264 -pix_fmt yuv420p output.mp4
```

## Convertire WEBP animato in MP4
```bash
ffmpeg -i input.webp -c:v libx264 -pix_fmt yuv420p -r 16 output.mp4
```

## Aggiungere audio a video
```bash
# Audio esatto come video
ffmpeg -i video.mp4 -i audio.wav -c:v copy -c:a aac -shortest output.mp4

# Audio con volume regolato
ffmpeg -i video.mp4 -i audio.wav -c:v copy -c:a aac -filter:a "volume=0.8" -shortest output.mp4
```

## Sovrapporre narrazione + musica di sottofondo
```bash
ffmpeg -i video.mp4 -i narrazione.wav -i musica.mp3 \
  -filter_complex "[1:a]volume=1.0[voice];[2:a]volume=0.3[music];[voice][music]amix=inputs=2:duration=first[aout]" \
  -map 0:v -map "[aout]" -c:v copy -c:a aac -shortest output.mp4
```

## Crossfade tra due clip
```bash
ffmpeg -i clip1.mp4 -i clip2.mp4 \
  -filter_complex "xfade=transition=fade:duration=0.5:offset=4.5" \
  -c:v libx264 -pix_fmt yuv420p output.mp4
```

Transizioni disponibili: fade, wipeleft, wiperight, wipeup, wipedown, dissolve, pixelize, diagtl, diagtr, smoothleft, smoothright

## Ridimensionare video
```bash
ffmpeg -i input.mp4 -vf "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2" output.mp4
```

## Aggiungere testo/sottotitoli
```bash
# Testo fisso
ffmpeg -i input.mp4 -vf "drawtext=text='Titolo':fontsize=48:fontcolor=white:x=(w-text_w)/2:y=h-80:borderw=2:bordercolor=black" output.mp4

# Sottotitoli da file SRT
ffmpeg -i input.mp4 -vf "subtitles=subs.srt:force_style='FontSize=24'" output.mp4
```

## Estrarre frame da video (per reference I2V)
```bash
# Frame specifico (es. secondo 2.5)
ffmpeg -i input.mp4 -ss 2.5 -frames:v 1 frame.png

# Tutti i frame
ffmpeg -i input.mp4 -r 1 frames/frame_%04d.png
```

## Creare GIF da video
```bash
ffmpeg -i input.mp4 -vf "fps=10,scale=480:-1:flags=lanczos" -loop 0 output.gif
```

## Output finale consigliato
- **MP4** con H.264 + AAC per massima compatibilità
- **Risoluzione**: mantenere quella dei clip generati (640x640 per WAN, 1024x1024 per immagini)
- **FPS**: 16 per WAN video, 24 per LivePortrait
