# Habbo Furni Watcher (Discord + GitHub Actions)

Cada 15 minutos revisa el `furnidata_xml` de 10 hoteles (com, sandbox, es, fi, com.br, nl, de, com.tr, it, fr).
Si aparece un `classname` nuevo en alguno, descarga su `.swf`
(`http://images.habbo.com/dcr/hof_furni/<revision>/<classname>.swf`), compone las imágenes en un
**WebP animado** y lo envía a Discord con: nombre, descripción, `canstandon`, `cansiton`, `canlayon`,
`canputstuffon`, `furniline`, `tradeable` y `recyclable`.

## Puesta en marcha
1. Sube esta carpeta a un repositorio de GitHub.
2. Discord: *Ajustes del canal → Integraciones → Webhooks → Nuevo webhook → Copiar URL*.
3. GitHub: *Settings → Secrets and variables → Actions → New repository secret*
   - Nombre: `DISCORD_WEBHOOK_URL`, valor: la URL del webhook.
4. *Settings → Actions → General → Workflow permissions* → **Read and write permissions**.
5. Pestaña **Actions → Habbo Furni Watcher → Run workflow**.
   La primera ejecución solo guarda la línea base (`state/known.json`) sin avisar; desde la segunda avisa de lo nuevo.

## Probar el renderizado en local
```bash
pip install -r requirements.txt
python -m bot.render exotic_r17_treefrog.swf salida.webp
```

## Notas
- `MAX_NOTIFY` (por defecto 25) limita los avisos por ejecución; el resto queda pendiente para la siguiente.
- Si un hotel falla puntualmente, se omite y se reintenta en la siguiente ejecución.
- Si un tag (p. ej. `tradeable`/`recyclable`) no existe en el XML del hotel, se muestra `N/D`.
- Variantes de color (`nombre*1`) reutilizan el `.swf` de `nombre`.
- GitHub desactiva los workflows programados tras 60 días sin actividad en repos públicos; el commit
  automático del estado ayuda a evitarlo.
