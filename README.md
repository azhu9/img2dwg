# Image → DXF

A small web app that turns PNG/JPG/WEBP images into clean, scaled vector DXF sketches for Onshape
(or any CAD tool). Straight edges become single lines, curves become smooth splines, stray dots are
removed, and the drawing is sized in inches and centered on the origin.

## Project layout

```
public/index.html   the page (upload, settings, live preview, download)
api/convert.py      Vercel Python function: POST /api/convert
api/_core.py        the conversion engine (Pillow + NumPy + Potrace + ezdxf)
requirements.txt    Python packages Vercel installs
vercel.json         function settings (60 s timeout)
dev_server.py       run locally without Vercel
```

## Deploy to Vercel

**Option A: GitHub (recommended)**
1. Create a new GitHub repository and push this folder to it.
2. On vercel.com, click **Add New → Project**, import the repository.
3. Leave Framework Preset as **Other** and leave the build settings empty. Click **Deploy**.

**Option B: Vercel CLI**
```
npm i -g vercel
cd img2cad-web
vercel          # first deploy (preview)
vercel --prod   # production
```

Vercel serves `public/` as the website and turns `api/convert.py` into a serverless function
automatically.

## Run locally

```
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python dev_server.py
```
Then open http://localhost:8000.

## API

`POST /api/convert?width=4` with the image as the raw request body.

| Query param  | Meaning                                                       | Default |
|--------------|---------------------------------------------------------------|---------|
| `width` / `height` | Final size in inches (give one)                         | longest side 4 |
| `mode`       | `fill` (solid shapes) or `edges` (line drawings / photos)     | `fill`  |
| `invert`     | `1` if the shape is light on a dark background                | `0`     |
| `use_alpha`  | `1` to trace a transparent PNG by its transparency (keeps white parts) | `0` |
| `min_area`   | Drop dots/holes smaller than this % of the image              | `0.05`  |
| `smooth`     | Corner roundness, 0 (sharp) – 1.33 (round)                    | `1.0`   |
| `line_tol`   | Straight-line snap in px; higher = more lines                 | `1.5`   |
| `threshold`  | 0–255 manual threshold (omit for automatic)                   | auto    |
| `blur`, `despeckle` | Pre-blur radius / speckle filter size in px            | `1`, `3`|

Response: `{ dxf, paths, nodes, width, height, profiles, lines, splines }`.

## Limits

- Images are resized in the browser to at most 2000 px before upload (Vercel caps uploads at 4.5 MB).
- Output is DXF. Onshape's **Insert DXF or DWG** reads it the same way as DWG.
