// Remove the off-white paper background from a scanned watercolor.
// For each pixel, alpha is a soft function of "darkness" and "chroma" — both low
// ⇒ paper (transparent), either high ⇒ paint (opaque). Trims fully-transparent
// borders afterwards.
//
// Usage (run from tmp-sharp/ after `npm i sharp@0.33.5`):
//   node ../scripts/remove-paper-bg.mjs "<input.jpg>" "<output.png>"
import sharp from 'sharp';
import { resolve } from 'node:path';

const [, , inArg, outArg] = process.argv;
if (!inArg || !outArg) {
  console.error('usage: node remove-paper-bg.mjs <input> <output>');
  process.exit(1);
}

const inPath = resolve(inArg);
const outPath = resolve(outArg);

// Tunables — increase to trim more paper, decrease to keep more subtle tones.
const FULL_PAPER_DARKNESS = 34;   // darkness (255-min(r,g,b)) below which pixels are pure paper
const FULL_PAPER_CHROMA   = 28;   // chroma (max-min) below which pixels are pure paper
const FULL_PAINT_DARKNESS = 75;   // beyond this ⇒ pure paint (opaque)
const FULL_PAINT_CHROMA   = 65;
const ALPHA_SNAP_BELOW    = 110;  // after soft key, drop weak alpha to 0 to kill halos

async function run() {
  const src = sharp(inPath).ensureAlpha();
  const { data, info } = await src.raw().toBuffer({ resolveWithObject: true });
  const { width, height } = info;
  const out = Buffer.alloc(data.length);

  for (let i = 0; i < data.length; i += 4) {
    const r = data[i], g = data[i + 1], b = data[i + 2];
    const minC = Math.min(r, g, b);
    const maxC = Math.max(r, g, b);
    const darkness = 255 - minC;
    const chroma = maxC - minC;

    let alpha;
    if (darkness <= FULL_PAPER_DARKNESS && chroma <= FULL_PAPER_CHROMA) {
      alpha = 0;
    } else if (darkness >= FULL_PAINT_DARKNESS || chroma >= FULL_PAINT_CHROMA) {
      alpha = 255;
    } else {
      const dScore = (darkness - FULL_PAPER_DARKNESS) / (FULL_PAINT_DARKNESS - FULL_PAPER_DARKNESS);
      const cScore = (chroma   - FULL_PAPER_CHROMA)   / (FULL_PAINT_CHROMA   - FULL_PAPER_CHROMA);
      alpha = Math.max(0, Math.min(255, Math.round(Math.max(dScore, cScore) * 255)));
      if (alpha < ALPHA_SNAP_BELOW) alpha = 0;
    }

    out[i]     = r;
    out[i + 1] = g;
    out[i + 2] = b;
    out[i + 3] = alpha;
  }

  await sharp(out, { raw: { width, height, channels: 4 } })
    .trim({ threshold: 1 })
    .png({ compressionLevel: 9 })
    .toFile(outPath);

  const meta = await sharp(outPath).metadata();
  console.log(`wrote ${outPath}  (${meta.width}×${meta.height})`);
}

run().catch((e) => { console.error(e); process.exit(1); });
