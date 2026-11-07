// Render the catalog card: assets/catalog-card.src.svg (editable, real text)
// -> assets/catalog-card.svg (text outlined to paths, no font needed)
// -> assets/catalog-card.png (1200x600, the catalog's 2:1 size).
//
// Usage: node scripts/render-card.mjs /path/to/node_modules/@resvg/resvg-js [/path/to/fonts...]
// Fonts default to DejaVu Sans, Sans Bold and Sans Mono Bold under /usr/share/fonts.
// No system fonts and no network: only the font files named here are loaded.
import { createRequire } from "node:module";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const [resvgPath, ...fontArgs] = process.argv.slice(2);
if (!resvgPath) {
  console.error("usage: node scripts/render-card.mjs <path to @resvg/resvg-js> [font files…]");
  process.exit(2);
}
const { Resvg } = createRequire(import.meta.url)(resvgPath);
const dejavu = "/usr/share/fonts/truetype/dejavu";
const fontFiles = fontArgs.length ? fontArgs : [
  `${dejavu}/DejaVuSans.ttf`, `${dejavu}/DejaVuSans-Bold.ttf`, `${dejavu}/DejaVuSansMono-Bold.ttf`,
];
const assets = join(dirname(fileURLToPath(import.meta.url)), "..", "assets");
const opts = { font: { loadSystemFonts: false, fontFiles, defaultFontFamily: "DejaVu Sans" } };

const source = readFileSync(join(assets, "catalog-card.src.svg"), "utf8");
const outlined = new Resvg(source, opts).toString();
if (/<text[\s>]/.test(outlined)) throw new Error("text was not outlined: a font is missing");
writeFileSync(join(assets, "catalog-card.svg"), outlined + "\n");

const png = new Resvg(outlined, { font: { loadSystemFonts: false } }).render();
if (png.width !== 1200 || png.height !== 600) throw new Error(`card is ${png.width}x${png.height}`);
writeFileSync(join(assets, "catalog-card.png"), png.asPng());
console.log(`catalog-card.svg (${outlined.length} bytes, text outlined), catalog-card.png ${png.width}x${png.height}`);
