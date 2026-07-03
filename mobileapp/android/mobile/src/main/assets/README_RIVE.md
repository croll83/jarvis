# Avatar Rive

Metti qui il file dell'avatar animato Rive:

    mobile/src/main/assets/jarvis_face.riv

Come ottenerlo:
1. Vai su rive.app, apri l'animazione scelta (es. "Robocat – Expressive Faces").
2. Aggiungila ai tuoi file / remixala, poi **Export → .riv** (runtime file).
3. Rinomina in `jarvis_face.riv` e mettilo in questa cartella.

Poi comunica allo sviluppo (dal pannello **State Machine** dell'editor Rive):
- il **nome della State Machine** (ora assunto: "State Machine 1")
- i **nomi degli input** che pilotano gli stati e il livello voce

così si affina `RiveFace.applyRiveInputs()`. Finché non combaciano, l'animazione
riproduce comunque il suo stato di default (autoplay). Se il file è assente,
l'app usa automaticamente l'avatar di fallback (`JarvisFace`).
